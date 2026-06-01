from sqlalchemy import (
    Column,
    ForeignKey,
    Enum,
    Integer,
    String,
    Float,
    UUID,
    DateTime,
    text,
    relationship,
    ForeignKeyConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, INTERVAL
from sqlalchemy.ext.declarative import declarative_base
from enum import Enum

Base = declarative_base()


# Custom Enums for state management
class SessionState(Enum):
    WAITING = "Waiting"
    OPEN = "Open"
    CLOSED = "Closed"


class TaskState(Enum):
    WAITING = "Waiting"
    RUNNING = "Running"
    FAILED = "Failed"
    FINISHED = "Finished"
    CANCELLED = "Cancelled"


# Define the models


class ConsumptionEntity(Base):
    __tablename__ = "ConsumptionEntities"

    LexisLocationName = Column(String(255), primary_key=True)
    LexisProject = Column(String(255), primary_key=True)
    LexisResourceName = Column(String(255), primary_key=True)
    CollectorName = Column(String(255), nullable=False)
    LexisUserId = Column(UUID, nullable=False)
    Consumption = Column(Float, nullable=False, default=0.0)
    ConsumptionFactor = Column(Float, nullable=False, default=1.0)

    sessions = relationship(
        "Session", backref="consumption_entity", cascade="all, delete-orphan"
    )
    tasks = relationship(
        "Task", backref="consumption_entity", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "Sessions"

    SessionId = Column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    LexisLocationName = Column(String(255), nullable=False)
    LexisProject = Column(String(255), nullable=False)
    LexisResourceName = Column(String(255), nullable=False)
    FromDatetime = Column(DateTime, nullable=False)
    ToDatetime = Column(DateTime)
    State = Column(Enum(SessionState), nullable=False, default=SessionState.WAITING)

    # Composite foreign key
    __table_args__ = (
        ForeignKeyConstraint(
            columns=[LexisLocationName, LexisProject, LexisResourceName],
            refcolumns=[
                "ConsumptionEntities.LexisLocationName",
                "ConsumptionEntities.LexisProject",
                "ConsumptionEntities.LexisResourceName",
            ],
        ),
    )

    tasks = relationship("Task", backref="session", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "Tasks"

    TaskId = Column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    LexisLocationName = Column(String(255), nullable=False)
    LexisProject = Column(String(255), nullable=False)
    LexisResourceName = Column(String(255), nullable=False)
    SessionId = Column(UUID, ForeignKey("Sessions.SessionId"), nullable=False)
    HeappeId = Column(Integer)
    IQMJobId = Column(UUID)
    State = Column(Enum(TaskState), nullable=False, default=TaskState.WAITING)

    # Composite foreign key
    __table_args__ = (
        ForeignKeyConstraint(
            columns=[LexisLocationName, LexisProject, LexisResourceName],
            refcolumns=[
                "ConsumptionEntities.LexisLocationName",
                "ConsumptionEntities.LexisProject",
                "ConsumptionEntities.LexisResourceName",
            ],
        ),
    )


class ResourceConsumptionSummary(Base):
    __tablename__ = "ResourceConsumptionSummaries"

    LexisLocationName = Column(String(255), primary_key=True)
    LexisResourceName = Column(String(255), primary_key=True)
    TotalCalculatedConsumption = Column(Float, nullable=False, default=0.0)

    __table_args__ = {"read_only": True}
