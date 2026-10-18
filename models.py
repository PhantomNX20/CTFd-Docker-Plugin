from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from CTFd.models import db
from CTFd.models import Challenges


class ContainerChallengeModel(Challenges):
    __tablename__ = "container_challenge_model"
    __mapper_args__ = {"polymorphic_identity": "container"}
    id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )
    image = db.Column(db.Text)
    port = db.Column(db.Integer)
    command = db.Column(db.Text, default="")
    volumes = db.Column(db.Text, default="")

    # Dynamic challenge properties (only if not already on Challenges in newer CTFd)
    if not hasattr(Challenges, "initial"):
        initial = db.Column(db.Integer, default=0)
    if not hasattr(Challenges, "minimum"):
        minimum = db.Column(db.Integer, default=0)
    if not hasattr(Challenges, "decay"):
        decay = db.Column(db.Integer, default=0)

    def __init__(self, *args, **kwargs):
        super(ContainerChallengeModel, self).__init__(**kwargs)
        initial = kwargs.get("initial", kwargs.get("value", 0))
        try:
            self.value = int(initial) if initial is not None else 0
        except (ValueError, TypeError):
            self.value = 0


class ContainerInfoModel(db.Model):
    __tablename__ = "container_info_model"
    __mapper_args__ = {"polymorphic_identity": "container_info"}
    container_id = db.Column(db.String(512), primary_key=True)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE")
    )
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    port = db.Column(db.Integer)
    timestamp = db.Column(db.Integer)
    expires = db.Column(db.Integer)
    team = relationship("Teams", foreign_keys=[team_id])
    user = relationship("Users", foreign_keys=[user_id])
    challenge = relationship(ContainerChallengeModel,
                             foreign_keys=[challenge_id])


class ContainerSettingsModel(db.Model):
    __tablename__ = "container_settings_model"
    __mapper_args__ = {"polymorphic_identity": "container_settings"}
    key = db.Column(db.String(512), primary_key=True)
    value = db.Column(db.Text)

