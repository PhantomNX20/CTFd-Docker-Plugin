from __future__ import division

import time
import json
import datetime
import math

from flask import Blueprint, request, Flask, render_template, url_for, redirect, flash

from CTFd.models import db, Solves
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from CTFd.utils.decorators import authed_only, admins_only, during_ctf_time_only, ratelimit, require_verified_emails
from CTFd.utils.user import get_current_user, get_current_team
from CTFd.utils.modes import get_model, get_mode, TEAMS_MODE, USERS_MODE

from .models import ContainerChallengeModel, ContainerInfoModel, ContainerSettingsModel
from .container_manager import ContainerManager, ContainerException


class ContainerChallenge(BaseChallenge):
    id = "container" 
    name = "container"
    templates = {
        "create": "/plugins/containers/assets/create.html",
        "update": "/plugins/containers/assets/update.html",
        "view": "/plugins/containers/assets/view.html",
    }
    scripts = {
        "create": "/plugins/containers/assets/create.js",
        "update": "/plugins/containers/assets/update.js",
        "view": "/plugins/containers/assets/view.js",
    }
    route = "/plugins/containers/assets/"
    challenge_model = ContainerChallengeModel

    @classmethod
    def read(cls, challenge):
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "image": challenge.image,
            "port": challenge.port,
            "command": challenge.command,
            "volumes": getattr(challenge, "volumes", "") or "",
            "initial": challenge.initial,
            "decay": challenge.decay,
            "minimum": challenge.minimum,
            "description": challenge.description,
            "connection_info": challenge.connection_info,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data

    @classmethod
    def calculate_value(cls, challenge):
        Model = get_model()
        solve_count = (
            Solves.query.join(Model, Solves.account_id == Model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                Model.hidden == False,
                Model.banned == False,
            )
            .count()
        )
        if solve_count != 0:
            solve_count -= 1

        try:
            initial = int(challenge.initial) if challenge.initial is not None else 0
        except (ValueError, TypeError):
            initial = 0

        try:
            minimum = int(challenge.minimum) if challenge.minimum is not None else 0
        except (ValueError, TypeError):
            minimum = 0

        try:
            decay = int(challenge.decay) if challenge.decay is not None else 0
        except (ValueError, TypeError):
            decay = 0

        if decay <= 0:
            challenge.value = initial
            db.session.commit()
            return challenge

        value = (
            ((minimum - initial) / (decay ** 2))
            * (solve_count ** 2)
        ) + initial

        value = math.ceil(value)
        if value < minimum:
            value = minimum

        challenge.value = value
        db.session.commit()
        return challenge

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json() or {}
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        else:
            data = dict(data)

        # Convert numeric fields to integers safely
        for field in ("initial", "minimum", "decay", "port", "value"):
            if field in data and data[field] is not None and str(data[field]).strip() != "":
                try:
                    data[field] = int(data[field])
                except (ValueError, TypeError):
                    pass

        # If value is not provided, initialize it from initial
        if "initial" in data and ("value" not in data or data["value"] is None):
            data["value"] = data["initial"]

        # Only pass valid database column keys to model constructor
        valid_keys = {c.key for c in cls.challenge_model.__table__.columns}
        for mapper in cls.challenge_model.__mapper__.iterate_to_root():
            valid_keys.update(c.key for c in mapper.columns)

        model_data = {k: v for k, v in data.items() if k in valid_keys}

        challenge = cls.challenge_model(**model_data)
        db.session.add(challenge)
        db.session.commit()

        return cls.calculate_value(challenge)

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json() or {}
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        else:
            data = dict(data)

        valid_keys = {c.key for c in cls.challenge_model.__table__.columns}
        for mapper in cls.challenge_model.__mapper__.iterate_to_root():
            valid_keys.update(c.key for c in mapper.columns)

        for attr, value in data.items():
            if attr not in valid_keys or attr in ("id", "type"):
                continue

            if attr in ("initial", "minimum", "decay", "port", "value"):
                if value is not None and str(value).strip() != "":
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        pass
                else:
                    value = 0 if attr != "port" else None

            setattr(challenge, attr, value)

        db.session.commit()
        return cls.calculate_value(challenge)

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)
        cls.calculate_value(challenge)


def settings_to_dict(settings):
    return {setting.key: setting.value for setting in settings}


def check_and_migrate_db(app: Flask):
    with app.app_context():
        try:
            from sqlalchemy import inspect, text
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            if "container_info_model" in tables:
                columns = [c["name"] for c in inspector.get_columns("container_info_model")]
                if "user_id" not in columns:
                    with db.engine.connect() as conn:
                        try:
                            conn.execute(text("ALTER TABLE container_info_model ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"))
                        except Exception:
                            conn.execute(text("ALTER TABLE container_info_model ADD COLUMN user_id INTEGER"))
                        try:
                            conn.commit()
                        except Exception:
                            pass
        except Exception as e:
            try:
                app.logger.warning(f"[CTFd-Docker-Plugin] DB Migration note: {e}")
            except Exception:
                pass


def load(app: Flask):
    app.db.create_all()
    check_and_migrate_db(app)
    CHALLENGE_CLASSES["container"] = ContainerChallenge
    register_plugin_assets_directory(app, base_path="/plugins/containers/assets/")

    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    container_manager = ContainerManager(container_settings, app)

    containers_bp = Blueprint(
        'containers', __name__, template_folder='templates', static_folder='assets', url_prefix='/containers')

    @containers_bp.app_template_filter("format_time")
    def format_time_filter(unix_seconds):
        return datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.timezone.utc).isoformat()

    def get_current_container_identity():
        user = get_current_user()
        if not user:
            return None, None, "User not found"

        try:
            mode = get_mode()
        except Exception:
            mode = TEAMS_MODE if getattr(user, "team", None) is not None else USERS_MODE

        if mode == TEAMS_MODE:
            team = get_current_team() or getattr(user, "team", None)
            if not team:
                return None, None, "Devi far parte di un team per avviare il container."
            return user, team, None
        else:
            return user, None, None

    def get_running_container(chal_id, user=None, team=None):
        try:
            mode = get_mode()
        except Exception:
            mode = TEAMS_MODE if team is not None else USERS_MODE

        if mode == TEAMS_MODE and team:
            return ContainerInfoModel.query.filter_by(
                challenge_id=chal_id, team_id=team.id
            ).first()
        elif user:
            return ContainerInfoModel.query.filter_by(
                challenge_id=chal_id, user_id=user.id
            ).first()
        return None

    def kill_container(container_id):
        container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
        if container:
            try:
                container_manager.kill_container(container_id)
            except ContainerException:
                return {"error": "Docker error during kill."}
            db.session.delete(container)
            db.session.commit()
        return {"success": "Container killed"}

    def renew_container(chal_id, user=None, team=None):
        challenge = ContainerChallenge.challenge_model.query.filter_by(id=chal_id).first()
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        running_container = get_running_container(challenge.id, user=user, team=team)
        if running_container is None:
            return {"error": "Container not found."}

        expires = int(time.time() + container_manager.expiration_seconds)
        running_container.expires = expires
        db.session.commit()
        return {"success": "Container renewed", "expires": expires}

    def create_container(chal_id, user=None, team=None):
        challenge = ContainerChallenge.challenge_model.query.filter_by(id=chal_id).first()
        if challenge is None:
            return {"error": "Challenge not found"}, 400

        # Verifica se esiste già un container per il team o per l'utente
        running_container = get_running_container(challenge.id, user=user, team=team)

        if running_container:
            try:
                if container_manager.is_container_running(running_container.container_id):
                    return json.dumps({
                        "status": "already_running",
                        "hostname": container_manager.settings.get("docker_hostname", ""),
                        "port": running_container.port,
                        "expires": running_container.expires
                    })
                else:
                    db.session.delete(running_container)
                    db.session.commit()
            except ContainerException as err:
                return {"error": str(err)}, 500

        # Creazione nuovo container
        try:
            # Chiamata al manager che restituisce la porta scelta randomicamente
            created_container = container_manager.create_container(
                challenge.image, challenge.port, challenge.command, challenge.volumes)
            
            # Recupero la porta salvata nell'attributo personalizzato del manager
            assigned_host_port = getattr(created_container, 'assigned_port', None)
            
            # Fallback se assegnazione diretta fallisce
            if assigned_host_port is None:
                assigned_host_port = container_manager.get_container_port(created_container.id)

        except ContainerException as err:
            return {"error": str(err)}

        if assigned_host_port is None:
            return json.dumps({"status": "error", "error": "Could not assign a port"})

        expires = int(time.time() + container_manager.expiration_seconds)

        team_id = team.id if team else None
        user_id = user.id if user else None

        # Salvataggio nel database
        new_db_entry = ContainerInfoModel(
            container_id=created_container.id,
            challenge_id=challenge.id,
            team_id=team_id,
            user_id=user_id,
            port=assigned_host_port,
            timestamp=int(time.time()),
            expires=expires
        )
        db.session.add(new_db_entry)
        db.session.commit()

        return json.dumps({
            "status": "created",
            "hostname": container_manager.settings.get("docker_hostname", ""),
            "port": assigned_host_port,
            "expires": expires
        })

    # --- ROUTES API ---
    @containers_bp.route('/api/request', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_request_container():
        if not request.json or "chal_id" not in request.json:
            return {"error": "Invalid request"}, 400
        user, team, err = get_current_container_identity()
        if err:
            return {"error": err}, 400
        return create_container(request.json.get("chal_id"), user=user, team=team)

    @containers_bp.route('/api/renew', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_renew_container():
        if not request.json or "chal_id" not in request.json:
            return {"error": "Invalid request"}, 400
        user, team, err = get_current_container_identity()
        if err:
            return {"error": err}, 400
        return renew_container(request.json.get("chal_id"), user=user, team=team)

    @containers_bp.route('/api/reset', methods=['POST'])
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    @ratelimit(method="POST", limit=6, interval=60)
    def route_restart_container():
        if not request.json or "chal_id" not in request.json:
            return {"error": "Invalid request"}, 400
        user, team, err = get_current_container_identity()
        if err:
            return {"error": err}, 400
        chal_id = request.json.get("chal_id")
        running_container = get_running_container(chal_id, user=user, team=team)
        if running_container:
            kill_container(running_container.container_id)
        return create_container(chal_id, user=user, team=team)

    @containers_bp.route('/api/stop', methods=['POST'])
    @authed_only
    def route_stop_container():
        if not request.json or "chal_id" not in request.json:
            return {"error": "Invalid request"}, 400
        user, team, err = get_current_container_identity()
        if err:
            return {"error": err}, 400
        chal_id = request.json.get("chal_id")
        running_container = get_running_container(chal_id, user=user, team=team)
        if running_container:
            return kill_container(running_container.container_id)
        return {"error": "No container found"}, 400

    @containers_bp.route('/api/kill', methods=['POST'])
    @admins_only
    def route_kill_container():
        return kill_container(request.json.get("container_id"))

    @containers_bp.route('/api/purge', methods=['POST'])
    @admins_only
    def route_purge_containers():
        containers = ContainerInfoModel.query.all()
        for c in containers:
            kill_container(c.container_id)
        return {"success": "Purged all"}, 200

    @containers_bp.route('/api/images', methods=['GET'])
    @admins_only
    def route_get_images():
        try:
            images = container_manager.get_images()
            return {"images": images or []}
        except (ContainerException, Exception) as err:
            return {"error": str(err)}

    @containers_bp.route('/api/settings/update', methods=['POST'])
    @admins_only
    def route_update_settings():
        keys = ["docker_base_url", "docker_hostname", "container_expiration", "container_maxmemory", "container_maxcpu", "container_min_port", "container_max_port"]
        for key in keys:
            setting = ContainerSettingsModel.query.filter_by(key=key).first()
            val = request.form.get(key)
            if setting:
                setting.value = val
            else:
                db.session.add(ContainerSettingsModel(key=key, value=val))
        db.session.commit()
        container_manager.settings = settings_to_dict(ContainerSettingsModel.query.all())
        try:
            container_manager.initialize_connection(container_manager.settings, app)
        except ContainerException as err:
            flash(str(err), "error")
            return redirect(url_for(".route_containers_settings"))
        return redirect(url_for(".route_containers_dashboard"))

    @containers_bp.route('/dashboard', methods=['GET'])
    @admins_only
    def route_containers_dashboard():
        running_containers = ContainerInfoModel.query.order_by(ContainerInfoModel.timestamp.desc()).all()
        connected = False
        try:
            connected = container_manager.is_connected()
        except Exception:
            pass
        for c in running_containers:
            try:
                c.is_running = container_manager.is_container_running(c.container_id)
            except Exception:
                c.is_running = False
        return render_template('container_dashboard.html', containers=running_containers, connected=connected)

    @containers_bp.route('/settings', methods=['GET'])
    @admins_only
    def route_containers_settings():
        return render_template('container_settings.html', settings=container_manager.settings)

    app.register_blueprint(containers_bp)