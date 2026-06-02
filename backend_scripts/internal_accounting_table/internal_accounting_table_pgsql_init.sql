-- Create custom ENUM types for state management
CREATE TYPE session_state AS ENUM ('Waiting', 'Open', 'Closed');
CREATE TYPE task_state AS ENUM ('Waiting', 'Running', 'Failed', 'Finished', 'Cancelled');

-- 1. Create ConsumptionEntities Table
CREATE TABLE IF NOT EXISTS ConsumptionEntities (
    ConsumptionId UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    LexisLocationName VARCHAR(255) NOT NULL,
    LexisProject VARCHAR(255) NOT NULL,
    LexisResourceName VARCHAR(255) NOT NULL,
    CollectorName VARCHAR(255) NOT NULL,
    LexisUserId UUID NOT NULL,
    Consumption DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ConsumptionFactor DOUBLE PRECISION NOT NULL DEFAULT 1.0
);

-- 2. Create Sessions Table (Omitted redundant Lexis* columns)
CREATE TABLE IF NOT EXISTS Sessions (
    SessionId UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ConsumptionId UUID NOT NULL,
    FromDatetime TIMESTAMP WITH TIME ZONE NOT NULL,
    ToDatetime TIMESTAMP WITH TIME ZONE,
    State session_state NOT NULL DEFAULT 'Waiting',
    CONSTRAINT fk_sessions_consumption 
        FOREIGN KEY (ConsumptionId)
        REFERENCES ConsumptionEntities(ConsumptionId)
        ON DELETE CASCADE
);

-- 3. Create Tasks Table (Omitted redundant Lexis* columns)
CREATE TABLE IF NOT EXISTS Tasks (
    TaskId UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ConsumptionId UUID NOT NULL,
    SessionId UUID DEFAULT NULL,
    HeappeId INTEGER,
    IQMJobId UUID,
    State task_state NOT NULL DEFAULT 'Waiting',
    CONSTRAINT fk_tasks_consumption 
        FOREIGN KEY (ConsumptionId)
        REFERENCES ConsumptionEntities(ConsumptionId)
        ON DELETE CASCADE,
    CONSTRAINT fk_tasks_sessions 
        FOREIGN KEY (SessionId)
        REFERENCES Sessions(SessionId)
        ON DELETE CASCADE
);

-- Performance Indexes (As requested: explicit indexing on location and resource names)
CREATE INDEX idx_consumption_location ON ConsumptionEntities(LexisLocationName);
CREATE INDEX idx_consumption_resource ON ConsumptionEntities(LexisResourceName);
CREATE INDEX idx_consumption_loc_res ON ConsumptionEntities(LexisLocationName, LexisResourceName);

-- Foreign Key Lookup Optimization Indexes
CREATE INDEX idx_sessions_consumption ON Sessions(ConsumptionId);
CREATE INDEX idx_tasks_consumption ON Tasks(ConsumptionId);
CREATE INDEX idx_tasks_session ON Tasks(SessionId);


-- 4. Summary Aggregations (No changes required here, reads directly from ConsumptionEntities)
CREATE TABLE IF NOT EXISTS ResourceConsumptionSummaries (
    LexisLocationName VARCHAR(255),
    LexisResourceName VARCHAR(255),
    TotalCalculatedConsumption DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (LexisLocationName, LexisResourceName)
);

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