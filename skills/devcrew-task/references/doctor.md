# Project doctor

`doctor --project PATH` reads required project directories, context and ten role TOMLs. It validates required role identity and instruction fields, then reports actionable missing or malformed configuration. Valid model customizations do not fail merely because they differ from package defaults.

Doctor does not write or repair files, inspect global settings, prove the model is available, or claim tests passed. Initialization conflicts are reported by `init`. Compare intended templates manually before changing a customized role.
