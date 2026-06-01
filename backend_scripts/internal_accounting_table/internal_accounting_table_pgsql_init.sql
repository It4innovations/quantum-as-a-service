-- Create custom ENUM types for state management
CREATE TYPE session_state AS ENUM ('Waiting', 'Open', 'Closed');
CREATE TYPE task_state AS ENUM ('Waiting', 'Running', 'Failed', 'Finished', 'Cancelled');

-- 1. Create ConsumptionEntities Table
CREATE TABLE IF NOT EXISTS ConsumptionEntities (
    LexisLocationName VARCHAR(255) NOT NULL,
    LexisProject VARCHAR(255) NOT NULL,
    LexisResourceName VARCHAR(255) NOT NULL,
    CollectorName VARCHAR(255) NOT NULL,
    LexisUserId UUID NOT NULL,
    Consumption DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ConsumptionFactor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    PRIMARY KEY (LexisLocationName, LexisResourceName, LexisProject)
);

-- 2. Create Sessions Table
CREATE TABLE IF NOT EXISTS Sessions (
    SessionId UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Changed to match composite primary key from ConsumptionEntities
    LexisLocationName VARCHAR(255) NOT NULL,
    LexisProject VARCHAR(255) NOT NULL,
    LexisResourceName VARCHAR(255) NOT NULL,
    FromDatetime TIMESTAMP WITH TIME ZONE NOT NULL,
    ToDatetime TIMESTAMP WITH TIME ZONE,
    State session_state NOT NULL DEFAULT 'Waiting',
    CONSTRAINT fk_sessions_consumption 
        FOREIGN KEY (LexisLocationName, LexisProject, LexisResourceName)
        REFERENCES ConsumptionEntities(LexisLocationName, LexisProject, LexisResourceName)
        ON DELETE CASCADE
);

-- 3. Create Tasks Table
CREATE TABLE IF NOT EXISTS Tasks (
    TaskId UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Changed to match composite primary key from ConsumptionEntities
    LexisLocationName VARCHAR(255) NOT NULL,
    LexisProject VARCHAR(255) NOT NULL,
    LexisResourceName VARCHAR(255) NOT NULL,
    SessionId UUID NOT NULL,
    HeappeId INTEGER,
    IQMJobId UUID,
    State task_state NOT NULL DEFAULT 'Waiting',
    CONSTRAINT fk_tasks_consumption 
        FOREIGN KEY (LexisLocationName, LexisProject, LexisResourceName)
        REFERENCES ConsumptionEntities(LexisLocationName, LexisProject, LexisResourceName)
        ON DELETE CASCADE,
    CONSTRAINT fk_tasks_sessions 
        FOREIGN KEY (SessionId)
        REFERENCES Sessions(SessionId)
        ON DELETE CASCADE
);

-- Performance Indexes
CREATE INDEX idx_consumption_resource ON ConsumptionEntities(LexisResourceName);
CREATE INDEX idx_tasks_session ON Tasks(SessionId);

-- This table holds the pre-calculated, aggregated values
CREATE TABLE IF NOT EXISTS ResourceConsumptionSummaries (
    LexisLocationName VARCHAR(255),
    LexisResourceName VARCHAR(255),
    TotalCalculatedConsumption DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (LexisLocationName, LexisResourceName)
);

-- The optimized view simply reads directly from the summary table
CREATE OR REPLACE VIEW v_lexis_resource_consumption AS
SELECT
    LexisResourceName,
    TotalCalculatedConsumption
FROM ResourceConsumptionSummaries;

CREATE OR REPLACE FUNCTION maintain_resource_consumption_summary()
RETURNS TRIGGER AS $$
BEGIN
    -- 1. Handle Deletions or Updates (Subtract old value)
    IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
        UPDATE ResourceConsumptionSummaries 
        SET TotalCalculatedConsumption = TotalCalculatedConsumption - (OLD.Consumption * OLD.ConsumptionFactor)
        WHERE LexisLocationName = OLD.LexisLocationName AND LexisResourceName = OLD.LexisResourceName;
    END IF;

    -- 2. Handle Inserts or Updates (Add new value)
    IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        INSERT INTO ResourceConsumptionSummaries (LexisLocationName, LexisResourceName, TotalCalculatedConsumption)
        VALUES (NEW.LexisLocationName, NEW.LexisResourceName, (NEW.Consumption * NEW.ConsumptionFactor))
        ON CONFLICT (LexisLocationName, LexisResourceName)
        DO UPDATE SET
                TotalCalculatedConsumption = ResourceConsumptionSummaries.TotalCalculatedConsumption + EXCLUDED.TotalCalculatedConsumption;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_consumption_changes
AFTER INSERT OR UPDATE OF Consumption, ConsumptionFactor, LexisResourceName, LexisLocationName, LexisProject 
OR DELETE 
ON ConsumptionEntities
FOR EACH ROW
EXECUTE FUNCTION maintain_resource_consumption_summary();