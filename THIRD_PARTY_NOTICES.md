# Third-party components

The Orcbro source code in this repository is MIT-licensed (see `LICENSE`). The container image additionally bundles
the following third-party components, which keep their own licenses.

| Component | Used for | License | Source |
|---|---|---|---|
| OrcaSlicer 2.5.1 (Linux AppImage, unmodified) | Slicing via its command line, run as a separate program | AGPL-3.0 | https://github.com/OrcaSlicer/OrcaSlicer/tree/v2.5.1 (profiles and printer data from commit `eef7710cb102fdb51a9bcdbd9efd61047f44774a`) |
| OrcaSlicer system profiles and `resources/printers` | Machine, process and filament presets; printer capabilities | AGPL-3.0 | same repository and commit as above |
| HMS and error texts (`hms_de.json.gz`, `hms_en.json.gz`) | Plain-text printer messages | MIT (collected by ha-bambulab; texts by Bambu Lab) | https://github.com/greghesp/ha-bambulab |
| Error dialog buttons per message code (`bridge/error_actions.json`, in this repository) | Recovery actions for printer messages | Data from Bambu Lab's HMS action tables, as used by Bambu Studio (`resources/hms`) | https://github.com/bambulab/BambuStudio |
| FFmpeg (Ubuntu package) | RTSPS camera of X1/P2S/H2/X2D printers | LGPL-2.1+/GPL | https://ffmpeg.org, Ubuntu `ffmpeg` package |
| Python packages (`requirements.txt`) | Runtime | see each package (MIT, Apache-2.0, BSD, LGPL for `zeroconf`) | PyPI |

**Corresponding source for AGPL components:** Orcbro uses the unmodified upstream OrcaSlicer release. The exact
source is the upstream tag and commit above; the `Dockerfile` downloads exactly these artifacts and verifies their
SHA-256 checksums. If you need a copy of the corresponding source and cannot obtain it upstream, open an issue in this
repository and we will provide it.

Bambu Lab, Bambu Studio and related names are trademarks of their respective owners. Orcbro and Bambro are
independent projects and are not affiliated with or endorsed by Bambu Lab or the OrcaSlicer project.
