# Orcbro: Python service + extracted OrcaSlicer CLI AppImage + system profiles from the OrcaSlicer repository.
# Supports linux/amd64 and linux/arm64 (Raspberry Pi 5, ARM NAS); select the AppImage with TARGETARCH.
ARG UBUNTU=ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

# --- OrcaSlicer: download, verify and extract the AppImage ---
FROM ${UBUNTU} AS orca
ARG TARGETARCH
ARG ORCA_VERSION=2.5.1
ARG ORCA_SHA256_AMD64=073b8105c928356141db0ddbce88b81d52c7a9e2442f9baae0a9089c89ff04eb
ARG ORCA_SHA256_ARM64=1336bbae0359466632066697780f314a8564542f8278e9ea4cdf18ec61383fc0
# Profiles and printer models (resources/printers: per-model capabilities) use the same v2.5.1 release;
# the AppImage contains profiles only in packed .opc form.
ARG ORCA_COMMIT=eef7710cb102fdb51a9bcdbd9efd61047f44774a
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Extracted directories are root-owned 0700; the bridge runs as 10001, requiring a+rX.
RUN case "${TARGETARCH:-amd64}" in \
      amd64) asset="OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"; sum="${ORCA_SHA256_AMD64}" ;; \
      arm64) asset="OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V${ORCA_VERSION}.AppImage"; sum="${ORCA_SHA256_ARM64}" ;; \
      *) echo "Unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o orca.AppImage "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/${asset}" \
    && echo "${sum}  orca.AppImage" | sha256sum -c - \
    && chmod +x orca.AppImage && ./orca.AppImage --appimage-extract >/dev/null && mv squashfs-root /opt/orca \
    && chmod -R a+rX /opt/orca
RUN git init -q src && cd src \
    && git remote add origin https://github.com/OrcaSlicer/OrcaSlicer.git \
    && git sparse-checkout set --no-cone /resources/profiles/BBL/ /resources/printers/ \
         /resources/profiles/OrcaFilamentLibrary/ /resources/profiles/Creality/filament/ \
         /resources/profiles/Anycubic/filament/ \
    && git fetch -q --depth 1 --filter=blob:none origin "${ORCA_COMMIT}" && git checkout -q FETCH_HEAD \
    && mkdir -p /opt/orca-profiles/Creality /opt/orca-profiles/Anycubic \
    && cp -r resources/profiles/BBL resources/profiles/OrcaFilamentLibrary /opt/orca-profiles/ \
    && cp -r resources/profiles/Creality/filament /opt/orca-profiles/Creality/ \
    && cp -r resources/profiles/Anycubic/filament /opt/orca-profiles/Anycubic/ \
    && cp resources/printers/bambu_filament_ids.json /opt/orca-profiles/ \
    && mkdir -p /opt/orca-profiles/printers && cp resources/printers/*.json /opt/orca-profiles/printers/ \
    && find /opt/orca-profiles -name "*.png" -delete
# English/German printer messages from Bambu Lab, collected by ha-bambulab (MIT).
ARG HMS_COMMIT=d2793d837648967f631d34a5b2f468a94099bef0
ARG HMS_DE_SHA256=2c9be808772316f444731151b5660e3a119e853c97958ada42a2e1b59451f765
ARG HMS_EN_SHA256=84dfa0d6c585ccb720ed1d0558523cfc8ab2ef79f1671279100c784421b85192
RUN mkdir -p /opt/hms && cd /opt/hms \
    && base="https://raw.githubusercontent.com/greghesp/ha-bambulab/${HMS_COMMIT}/custom_components/bambu_lab/pybambu/hms_error_text" \
    && curl -fsSL -o hms_de.json.gz "$base/hms_de.json.gz" && curl -fsSL -o hms_en.json.gz "$base/hms_en.json.gz" \
    && printf '%s  hms_de.json.gz\n%s  hms_en.json.gz\n' "${HMS_DE_SHA256}" "${HMS_EN_SHA256}" | sha256sum -c - \
    && chmod -R a+rX /opt/hms

# --- Runtime ---
FROM ${UBUNTU}
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# The CLI loads GTK/WebKit at startup; Xvfb + Mesa software GL render 3MF thumbnails.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg python3 python3-venv \
      libgtk-3-0t64 libwebkit2gtk-4.1-0 libgl1 libegl1 libglu1-mesa libosmesa6 libgl1-mesa-dri \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bambu && useradd --uid 10001 --gid 10001 --no-create-home bambu
COPY --from=orca /opt/orca /opt/orca
COPY --from=orca /opt/orca-profiles /opt/orca-profiles
COPY --from=orca /opt/hms /opt/hms
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
WORKDIR /app
COPY bridge/ ./bridge/
# The service user owns the data directory; a named volume inherits ownership on first launch.
RUN mkdir -p /data && chown 10001:10001 /data
LABEL org.opencontainers.image.title="Orcbro" \
      org.opencontainers.image.description="Self-hosted bridge between Bambu Lab printers (LAN mode) and the Bambro iOS app" \
      org.opencontainers.image.source="https://github.com/blankkit/orcbro" \
      org.opencontainers.image.licenses="MIT AND AGPL-3.0-only"
USER 10001:10001
VOLUME /data
EXPOSE 8080 8443
ENTRYPOINT ["/opt/venv/bin/python", "-m", "bridge"]
