-- Allow one immutable Trip Intake to carry multiple revisions in the same room.
-- The primary key (intake_id, revision) remains the uniqueness authority.

ALTER TABLE trip_intake_revisions
    DROP CONSTRAINT IF EXISTS trip_intake_revisions_room_id_intake_id_key;
