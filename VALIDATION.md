# Publication validation

Checked on 2026-10-13. These results apply to the publication copy, not every historical snapshot.

Existing pytest suite: **7 passed, 4 subtests passed**. Live Paint automation and screen calibration were not exercised.

## Publication checks

Source files and retained history are scanned for credentials with Gitleaks and additional manual review of configuration, notebooks and private data. A specific upstream SHA-256 in Phone Studio is allowlisted as an integrity checksum, not a credential. This is a bounded review, not a guarantee that the projects are defect-free. Large runtime files, secrets and local account/session databases are excluded.
