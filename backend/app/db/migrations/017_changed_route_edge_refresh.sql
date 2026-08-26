-- P6: a provider call for changed route edges is an immutable, idempotent
-- creation command.  It never changes an existing itinerary revision or
-- evidence snapshot.

ALTER TABLE idempotent_creation_commands
    DROP CONSTRAINT IF EXISTS idempotent_creation_commands_operation_check;
ALTER TABLE idempotent_creation_commands
    ADD CONSTRAINT idempotent_creation_commands_operation_check CHECK (operation IN (
        'CREATE_IMPORT', 'CREATE_AUDIT', 'REFRESH_AUDIT', 'PROPOSE_REPAIRS',
        'GENERATE_TIPS', 'APPLY_TEMPLATE', 'PRE_TRIP_RECHECK',
        'REFRESH_CHANGED_ROUTE_EDGES'
    ));
