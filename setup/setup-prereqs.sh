#!/usr/bin/env bash
#
# Subway Builder Melbourne data-pipeline prerequisites — Linux/WSL side.
#
# Installs everything the depot map-making library + the demand pipeline
# need beyond Docker Desktop (which lives on the Windows host and is
# bridged into WSL by Docker's WSL2 integration).
#
# Tested on Ubuntu 24.04 inside WSL2. Re-runnable; each step skips if
# the target tool is already on PATH or already installed.
#
# Run as root (sudo) for the first invocation:
#     sudo bash /opt/subwaybuilder-melbourne/setup-prereqs.sh
#
# After it finishes, log out / `exec bash` so the new PATH and conda
# init take effect, then verify with the smoke tests at the end.

set -euo pipefail

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

step()  { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
ok()    { printf "    \033[0;32mOK\033[0m  %s\n" "$*"; }
skip()  { printf "    \033[0;90m--  %s (already installed)\033[0m\n" "$*"; }
warn()  { printf "    \033[0;33m!!\033[0m  %s\n" "$*"; }

# The non-root user who will own conda + depot. WSL Ubuntu default user
# is the first user created; fall back to ${SUDO_USER:-$USER}.
TARGET_USER="${SUDO_USER:-${USER:-$(id -un 1000 2>/dev/null || echo root)}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -z "$TARGET_HOME" ] && TARGET_HOME="/home/$TARGET_USER"

if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running under sudo..."
    exec sudo -E bash "$0" "$@"
fi

step "Target user: $TARGET_USER  (home: $TARGET_HOME)"

# ---------------------------------------------------------------------------
# 1. apt packages
# ---------------------------------------------------------------------------
step "apt packages (osmium, tippecanoe, java, jq, sqlite3, node, go, ...)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y

APT_PKGS=(
    build-essential
    ca-certificates
    curl
    git
    unzip
    jq
    sqlite3
    default-jdk        # Java 17+ for planetiler
    osmium-tool
    tippecanoe         # ships in Ubuntu universe; covers depot's needs
    nodejs
    npm
    golang-go
    python3-pip        # only for global pip if needed
)

apt-get install -y "${APT_PKGS[@]}"
ok "apt deps installed"

# ---------------------------------------------------------------------------
# 2. mapshaper (npm, global)
# ---------------------------------------------------------------------------
step "mapshaper (npm global)"
if command -v mapshaper >/dev/null 2>&1; then
    skip "mapshaper"
else
    npm install -g mapshaper
    ok "mapshaper installed"
fi

# ---------------------------------------------------------------------------
# 3. pmtiles (prebuilt binary from protomaps/go-pmtiles releases)
# ---------------------------------------------------------------------------
step "pmtiles CLI"
if command -v pmtiles >/dev/null 2>&1; then
    skip "pmtiles"
else
    # Auto-discover the latest Linux x86_64 release tarball URL from GitHub.
    PMTILES_URL="$(curl -fsSL https://api.github.com/repos/protomaps/go-pmtiles/releases/latest \
        | grep -oE 'https://[^"]+_Linux_x86_64\.tar\.gz' \
        | head -1 || true)"
    if [ -z "$PMTILES_URL" ]; then
        warn "Couldn't auto-discover pmtiles release URL; falling back to v1.30.2"
        PMTILES_URL="https://github.com/protomaps/go-pmtiles/releases/download/v1.30.2/go-pmtiles_1.30.2_Linux_x86_64.tar.gz"
    fi
    TMPDIR_PT="$(mktemp -d)"
    curl -fsSL -o "$TMPDIR_PT/pmtiles.tgz" "$PMTILES_URL"
    tar -C "$TMPDIR_PT" -xzf "$TMPDIR_PT/pmtiles.tgz"
    install -m 0755 "$TMPDIR_PT/pmtiles" /usr/local/bin/pmtiles
    rm -rf "$TMPDIR_PT"
    ok "pmtiles installed -> /usr/local/bin/pmtiles (from $PMTILES_URL)"
fi

# ---------------------------------------------------------------------------
# 4. planetiler.jar
# ---------------------------------------------------------------------------
step "planetiler.jar"
PLANETILER_JAR=/usr/local/lib/planetiler.jar
PLANETILER_BIN=/usr/local/bin/planetiler
if [ -f "$PLANETILER_JAR" ]; then
    skip "planetiler.jar"
else
    mkdir -p /usr/local/lib
    # Latest release jar — published as planetiler.jar on every tag.
    curl -fsSL -o "$PLANETILER_JAR" \
        https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar
    cat > "$PLANETILER_BIN" <<'EOF'
#!/usr/bin/env bash
exec java -jar /usr/local/lib/planetiler.jar "$@"
EOF
    chmod +x "$PLANETILER_BIN"
    # depot._validate_env looks up `planetiler.jar` via shutil.which() and
    # then calls `java -jar <that path>`, so the .jar itself must be on
    # PATH by its actual name — not behind a shell wrapper.
    ln -sf "$PLANETILER_JAR" /usr/local/bin/planetiler.jar
    ok "planetiler installed -> $PLANETILER_BIN + /usr/local/bin/planetiler.jar -> $PLANETILER_JAR"
fi

# ---------------------------------------------------------------------------
# 5. Miniconda (per-user, ~/miniconda3)
# ---------------------------------------------------------------------------
step "Miniconda"
CONDA_DIR="$TARGET_HOME/miniconda3"
if [ -d "$CONDA_DIR" ] && [ -x "$CONDA_DIR/bin/conda" ]; then
    skip "Miniconda at $CONDA_DIR"
else
    INSTALLER=/tmp/miniconda.sh
    curl -fsSL -o "$INSTALLER" \
        https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
    sudo -u "$TARGET_USER" bash "$INSTALLER" -b -p "$CONDA_DIR"
    rm -f "$INSTALLER"
    # conda init for bash so `conda activate` works in future shells.
    sudo -u "$TARGET_USER" bash -lc "$CONDA_DIR/bin/conda init bash" >/dev/null
    ok "Miniconda installed at $CONDA_DIR"
fi

# ---------------------------------------------------------------------------
# 6. depot conda env + package
# ---------------------------------------------------------------------------
step "depot conda env"

DEPOT_SRC="$TARGET_HOME/src/depot"
sudo -u "$TARGET_USER" mkdir -p "$TARGET_HOME/src"

if [ -d "$DEPOT_SRC/.git" ]; then
    skip "depot repo at $DEPOT_SRC (running git pull)"
    sudo -u "$TARGET_USER" git -C "$DEPOT_SRC" pull --ff-only
else
    sudo -u "$TARGET_USER" git clone https://github.com/Subway-Builder-Modded/depot.git "$DEPOT_SRC"
    ok "Cloned depot -> $DEPOT_SRC"
fi

# Create the env from depot's environment.yml, then pip-install depot itself.
# depot's environment.yml doesn't list pip, and on Ubuntu the host Python is
# PEP-668-protected, so an un-prefixed `pip` from inside the activated env
# falls through to the system pip and fails. Pin `pip` into the env and use
# `python -m pip` so it always resolves to the env's bundled pip.
sudo -u "$TARGET_USER" bash -lc "
    set -e
    source $CONDA_DIR/etc/profile.d/conda.sh
    if conda env list | awk '{print \$1}' | grep -qx depot; then
        echo '    -- depot conda env already exists; updating'
        conda env update -n depot -f $DEPOT_SRC/environment.yml --prune
    else
        conda env create -n depot -f $DEPOT_SRC/environment.yml
    fi
    conda install -n depot -y pip
    conda activate depot
    python -m pip install --upgrade pip
    python -m pip install -e $DEPOT_SRC
"
ok "depot env ready"

# ---------------------------------------------------------------------------
# 7. Demand-side Python deps (system pip, for demand_generator.py)
# ---------------------------------------------------------------------------
step "openpyxl (for demand_generator.py)"
sudo -u "$TARGET_USER" pip3 install --user --upgrade openpyxl >/dev/null
ok "openpyxl installed under $TARGET_USER"

# ---------------------------------------------------------------------------
# 8. Summary + smoke-test instructions
# ---------------------------------------------------------------------------
cat <<EOF

================================================================
 Linux-side prerequisites are in place.
================================================================

Open a fresh shell (\`exec bash\`) or log out/in so the new PATH and
conda init take effect.

Smoke test:

    docker --version          # bridged from Docker Desktop on Windows
    osmium --version
    tippecanoe --version
    java -version
    mapshaper --version
    pmtiles version
    planetiler --help | head -1
    conda --version
    conda activate depot && python -c "import depot; print('depot ok')"

If 'docker --version' is missing, open Docker Desktop on the Windows
host, go to Settings -> Resources -> WSL Integration, and enable the
toggle for this distro.

Then proceed with the pipeline (from subwaybuilder-melbourne-data/
on the Windows side, accessible at /mnt/d/SubwayBuilder_Mods/...):

    osmium extract -b 144.45,-38.40,145.65,-37.55 \\
        australia-latest.osm.pbf -o melbourne.osm.pbf
    conda activate depot && python build_basemap.py
    # OSRM extract/partition/customize via docker run ...
    python demand_generator.py --config melbourne.json --pbf melbourne.osrm

EOF
