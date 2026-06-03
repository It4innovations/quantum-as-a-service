import enum
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    String,
    Float,
    UUID,
    DateTime,
    text
)
from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


# Define custom Enums for state management
class SessionState(enum.Enum):
    Waiting = "Waiting"
    Open = "Open"
    Closed = "Closed"


class TaskState(enum.Enum):
    Waiting = "Waiting"
    Running = "Running"
    Failed = "Failed"
    Finished = "Finished"
    Cancelled = "Cancelled"


#####################
# Define the models #
#####################
class ConsumptionEntity(Base):
    __tablename__ = "consumptionentities"

    ConsumptionId = Column(
        UUID,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        name="consumptionid",
    )
    LexisLocationName = Column(String(255), nullable=False, name="lexislocationname")
    LexisProject = Column(String(255), nullable=False, name="lexisproject")
    LexisResourceName = Column(String(255), nullable=False, name="lexisresourcename")
    CollectorName = Column(String(255), nullable=False, name="collectorname")
    LexisUserId = Column(UUID, nullable=False, name="lexisuserid")
    Consumption = Column(Float, nullable=False, default=0.0, name="consumption")
    ConsumptionFactor = Column(
        Float, nullable=False, default=1.0, name="consumptionfactor"
    )

    # Explicit indexes defined via __table_args__ for faster lookups
    __table_args__ = (
        Index("idx_consumption_location", "lexislocationname"),
        Index("idx_consumption_resource", "lexisresourcename"),
        # Optional composite index if you often query by both together:
        Index("idx_consumption_loc_res", "lexislocationname", "lexisresourcename"),
    )

    sessions = relationship(
        "Session", backref="consumption_entity", cascade="all, delete-orphan"
    )
    tasks = relationship(
        "Task", backref="consumption_entity", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    SessionId = Column(
        UUID,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        name="sessionid",
    )
    # The only tie back to metadata is through this single FK
    ConsumptionId = Column(
        UUID,
        ForeignKey("consumptionentities.consumptionid", ondelete="CASCADE"),
        nullable=False,
        name="consumptionid",
    )
    FromDatetime = Column(DateTime, nullable=False, name="fromdatetime")
    ToDatetime = Column(DateTime, name="todatetime")
    State = Column(
        ENUM(SessionState, name="session_state", create_type=False),
        nullable=False,
        default=SessionState.Waiting,
        name="state",
    )

    tasks = relationship("Task", backref="session", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    TaskId = Column(
        UUID, primary_key=True, server_default=text("gen_random_uuid()"), name="taskid"
    )
    # Tied directly to its unique consumption entry
    ConsumptionId = Column(
        UUID,
        ForeignKey("consumptionentities.consumptionid", ondelete="CASCADE"),
        nullable=False,
        name="consumptionid",
    )
    SessionId = Column(
        UUID,
        ForeignKey("sessions.sessionid", ondelete="CASCADE"),
        nullable=True,
        default=None,
        name="sessionid",
    )
    HeappeId = Column(Integer, name="heappeid")
    IQMJobId = Column(UUID, name="iqmjobid")
    State = Column(
        ENUM(TaskState, name="task_state", create_type=False),
        nullable=False,
        default=TaskState.Waiting,
        name="state",
    )


class ResourceConsumptionSummary(Base):
    __tablename__ = "resourceconsumptionsummaries"

    LexisLocationName = Column(String(255), primary_key=True, name="lexislocationname")
    LexisResourceName = Column(String(255), primary_key=True, name="lexisresourcename")
    TotalCalculatedConsumption = Column(
        Float, nullable=False, default=0.0, name="totalcalculatedconsumption"
    )
