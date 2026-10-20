# Production release prerequisites

Complete these one-time repository/package settings before pushing the first
`v*` production tag:

1. Keep **immutable releases** enabled in repository Settings. The workflow
   verifies this and refuses to publish when it is disabled.
2. After the first GHCR packages are created, set both packages to **Public**:
   - `reactor-controller-supervisory`
   - `reactor-controller-grafana`
3. Re-run the failed tag workflow. It logs out of GHCR and pulls both images by
   digest without credentials before any GitHub Release is published.

Use an immutable `vX.Y.Z-rc.N` prerelease for the fresh-Pi acceptance run. The
release builder rewrites only the published bootstrap asset so it downloads the
matching prerelease assets. Publish the final `vX.Y.Z` tag only after the Pi
network, reboot, SSH, UI, and Grafana acceptance matrix passes.

A public GitHub repository does not automatically make linked container
packages public. The fresh-Pi installer has no GHCR credential, so this gate is
mandatory. Do not work around it by adding registry credentials to the
bootstrap script or the connection report.
