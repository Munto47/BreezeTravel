-- P8 (local): a pre-trip recheck is an explicit, idempotent creation command.
-- It remains local-only; this migration does not create any public/Beta state.

ALTER TABLE idempotent_creation_commands
    DROP CONSTRAINT IF EXISTS idempotent_creation_commands_operation_check;
ALTER TABLE idempotent_creation_commands
    ADD CONSTRAINT idempotent_creation_commands_operation_check CHECK (operation IN (
        'CREATE_IMPORT', 'CREATE_AUDIT', 'REFRESH_AUDIT', 'PROPOSE_REPAIRS',
        'GENERATE_TIPS', 'APPLY_TEMPLATE', 'PRE_TRIP_RECHECK'
    ));
