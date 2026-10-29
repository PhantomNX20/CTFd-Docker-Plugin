import atexit
import time
import json
import socket
import random

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
import docker
import paramiko.ssh_exception
import requests

from CTFd.models import db
from .models import ContainerInfoModel


class ContainerException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        if args:
            self.message = args[0]
        else:
            self.message = None

    def __str__(self) -> str:
        if self.message:
            return self.message
        else:
            return "Unknown Container Exception"


class ContainerManager:
    def __init__(self, settings, app):
        self.settings = settings
        self.client = None
        self.app = app

        # Default expiration is at least 10 minutes (600 seconds)
        try:
            exp_mins = int(settings.get("container_expiration") or 10)
            if exp_mins < 10:
                exp_mins = 10
            self.expiration_seconds = exp_mins * 60
        except (ValueError, TypeError):
            self.expiration_seconds = 10 * 60

        if settings.get("docker_base_url") is None or settings.get("docker_base_url") == "":
            return

        # Connect to the docker daemon
        try:
            self.initialize_connection(settings, app)
        except ContainerException:
            print("Docker could not initialize or connect.")
            return

    def _get_free_port(self):
        # 1. Recupera il range
        try:
            min_port = int(self.settings.get("container_min_port") or 10000)
        except (ValueError, TypeError):
            min_port = 10000

        try:
            max_port = int(self.settings.get("container_max_port") or 10150)
        except (ValueError, TypeError):
            max_port = 10150

        if min_port > max_port:
            min_port, max_port = max_port, min_port

        min_port = max(1, min(65535, min_port))
        max_port = max(1, min(65535, max_port))

        print(f"[DEBUG] Range porte: {min_port}-{max_port}")

        # 2. Prendi le porte già usate dal DB
        with self.app.app_context():
            used_ports_query = db.session.query(ContainerInfoModel.port).all()
            used_ports = {int(p[0]) for p in used_ports_query if p[0] is not None}

        # 3. Crea la lista e rimescolala
        all_ports = list(range(min_port, max_port + 1))
        random.shuffle(all_ports)

        # 4. Cerca la prima disponibile
        for port in all_ports:
            if port in used_ports:
                continue
                
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(('127.0.0.1', port)) != 0:
                    print(f"[DEBUG] Porta assegnata: {port}")
                    return port
                    
        raise ContainerException("Nessuna porta disponibile nel range!")

    def initialize_connection(self, settings, app) -> None:
        self.settings = settings
        self.app = app

        # Remove any leftover expiration schedulers
        try:
            self.expiration_scheduler.shutdown()
        except (SchedulerNotRunningError, AttributeError):
            # Scheduler was never running
            pass

        if settings.get("docker_base_url") is None:
            self.client = None
            return

        try:
            self.client = docker.DockerClient(
                base_url=settings.get("docker_base_url"))
        except (docker.errors.DockerException) as e:
            self.client = None
            raise ContainerException("CTFd could not connect to Docker")
        except TimeoutError as e:
            self.client = None
            raise ContainerException(
                "CTFd timed out when connecting to Docker")
        except paramiko.ssh_exception.NoValidConnectionsError as e:
            self.client = None
            raise ContainerException(
                "CTFd timed out when connecting to Docker: " + str(e))
        except paramiko.ssh_exception.AuthenticationException as e:
            self.client = None
            raise ContainerException(
                "CTFd had an authentication error when connecting to Docker: " + str(e))

        # Set up expiration scheduler (at least 10 minutes)
        try:
            exp_mins = int(settings.get("container_expiration") or 10)
            if exp_mins < 10:
                exp_mins = 10
            self.expiration_seconds = exp_mins * 60
        except (ValueError, TypeError):
            self.expiration_seconds = 10 * 60

        EXPIRATION_CHECK_INTERVAL = 5

        if self.expiration_seconds > 0:
            self.expiration_scheduler = BackgroundScheduler()
            self.expiration_scheduler.add_job(
                func=self.kill_expired_containers, args=(app,), trigger="interval", seconds=EXPIRATION_CHECK_INTERVAL)
            self.expiration_scheduler.start()

            # Shut down the scheduler when exiting the app
            atexit.register(lambda: self.expiration_scheduler.shutdown())

    # TODO: Fix this cause it doesn't work
    def run_command(func):
        def wrapper_run_command(self, *args, **kwargs):
            if self.client is None:
                try:
                    self.__init__(self.settings, self.app)
                except:
                    raise ContainerException("Docker is not connected")
            try:
                if self.client is None:
                    raise ContainerException("Docker is not connected")
                if self.client.ping():
                    return func(self, *args, **kwargs)
            except (paramiko.ssh_exception.SSHException, ConnectionError, requests.exceptions.ConnectionError) as e:
                # Try to reconnect before failing
                try:
                    self.__init__(self.settings, self.app)
                except:
                    pass
                raise ContainerException(
                    "Docker connection was lost. Please try your request again later.")
        return wrapper_run_command

    @run_command
    def kill_expired_containers(self, app: Flask):
        with app.app_context():
            containers: "list[ContainerInfoModel]" = ContainerInfoModel.query.all()

            for container in containers:
                delta_seconds = container.expires - int(time.time())
                if delta_seconds < 0:
                    try:
                        self.kill_container(container.container_id)
                    except ContainerException:
                        print(
                            "[Container Expiry Job] Docker is not initialized. Please check your settings.")

                    db.session.delete(container)
                    db.session.commit()

    @run_command
    def is_container_running(self, container_id: str) -> bool:
        container = self.client.containers.list(filters={"id": container_id})
        if len(container) == 0:
            return False
        return container[0].status == "running"

    @run_command
    def create_container(self, image: str, port: int, command: str, volumes: str):
        kwargs = {}
        
        # 1. Trova una porta libera nel range
        host_port = self._get_free_port()
        
        # 2. Imposta il limite disco (Storage Opt)
        # NOTA: Funziona solo con storage driver overlay2 (su XFS con pquota) o btrfs
        # storage_limit = self.settings.get("container_maxdisk", "256M")
        # kwargs["storage_opt"] = {"size": storage_limit}

        # Gestione Memoria e CPU (codice esistente)
        if self.settings.get("container_maxmemory"):
            try:
                mem_limit = int(self.settings.get("container_maxmemory"))
                if mem_limit > 0:
                    kwargs["mem_limit"] = f"{mem_limit}m"
            except ValueError:
                pass 

        if self.settings.get("container_maxcpu"):
            try:
                cpu_period = float(self.settings.get("container_maxcpu"))
                if cpu_period > 0:
                    kwargs["cpu_quota"] = int(cpu_period * 100000)
                    kwargs["cpu_period"] = 100000
            except ValueError:
                pass

        if volumes and str(volumes).strip() and str(volumes).strip() != "None":
            try:
                parsed_volumes = json.loads(volumes)
                if isinstance(parsed_volumes, dict):
                    kwargs["volumes"] = parsed_volumes
                else:
                    raise ContainerException("Volumes JSON must be an object/dict")
            except json.decoder.JSONDecodeError:
                raise ContainerException("Volumes JSON string is invalid")

        try:
            container = self.client.containers.run(
                image,
                ports={f"{port}/tcp": host_port}, 
                command=command,
                detach=True,
                auto_remove=True,
                **kwargs
            )
            # ATTENZIONE: Aggiungiamo la porta scelta all'oggetto container 
            # così il plugin può leggerla direttamente senza chiederla a Docker
            container.assigned_port = host_port
            return container
        
        except docker.errors.ImageNotFound:
            raise ContainerException("Docker image not found")
        except docker.errors.APIError as e:
            raise ContainerException(f"Errore Docker: {str(e)}")

    @run_command
    def get_container_port(self, container_id: str) -> "str|None":
        try:
            for port in list(self.client.containers.get(container_id).ports.values()):
                if port is not None:
                    return port[0]["HostPort"]
        except (KeyError, IndexError):
            return None

    @run_command
    def get_images(self) -> "list[str]|None":
        try:
            images = self.client.images.list()
        except (KeyError, IndexError):
            return []

        images_list = []
        for image in images:
            if len(image.tags) > 0:
                images_list.append(image.tags[0])

        images_list.sort()
        return images_list

    @run_command
    def kill_container(self, container_id: str):
        try:
            self.client.containers.get(container_id).kill()
        except docker.errors.NotFound:
            pass

    def is_connected(self) -> bool:
        try:
            self.client.ping()
        except:
            return False
        return True
