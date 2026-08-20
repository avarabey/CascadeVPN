-- Existing rows deliberately receive an empty binding and become invalid as soon
-- as the upgraded application starts using the current password-hash binding.
ALTER TABLE sessions ADD COLUMN password_binding TEXT NOT NULL DEFAULT '';
