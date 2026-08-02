# Private raw geometry

Place the original, read-only STEP input here after cloning. For this project the
configured path is `geometry/raw/model1.step`.

CAD files are deliberately excluded from Git. Transfer them through an approved
private channel, preserve the read-only source, and verify its SHA-256 with:

```powershell
Get-FileHash -Algorithm SHA256 geometry/raw/model1.step
```

The expected digest is recorded in `config/markers.toml`. Never edit or repair
the original STEP in place.
