#!/usr/bin/env bash
#
# publish.sh — Build the Teams media bridge for Windows x64 and zip it for install.ps1.
#
# Publishes src/TeamsMediaBridge framework-dependent (the VM gets the .NET 8 runtime from install.ps1)
# into out/ and zips the contents of out/ (flat, exe at the zip root) to teams-media-bridge.zip.
# Upload the zip to blob storage and pass its SAS URL to install.ps1 -PackageUrl.
#
# Usage: ./deploy/publish.sh [--dry-run] [-h|--help]
#
# Prerequisites:
#   - .NET 8 SDK (default: ~/.dotnet/dotnet; override with DOTNET=/path/to/dotnet)
#   - zip
set -euo pipefail

show_usage() {
    cat <<USAGE
Usage: $0 [--dry-run] [-h|--help]

Publishes TeamsMediaBridge for win-x64 (framework-dependent) to out/ and creates teams-media-bridge.zip.

Options:
  --dry-run    Print the commands without running them
  -h, --help   Show this help

Environment:
  DOTNET       dotnet executable (default: \$HOME/.dotnet/dotnet, else dotnet on PATH)
USAGE
}

DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        -h|--help) show_usage; exit 0 ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown option: $arg" >&2; show_usage >&2; exit 1 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${ROOT}/src/TeamsMediaBridge/TeamsMediaBridge.csproj"
OUT_DIR="${ROOT}/out"
ZIP_PATH="${ROOT}/teams-media-bridge.zip"

if [ -z "${DOTNET:-}" ]; then
    if [ -x "${HOME}/.dotnet/dotnet" ]; then DOTNET="${HOME}/.dotnet/dotnet"; else DOTNET="$(command -v dotnet || true)"; fi
fi
if [ -z "${DOTNET}" ] || [ ! -x "${DOTNET}" ]; then
    echo "ERROR: dotnet not found (set DOTNET=...)" >&2
    exit 1
fi
command -v zip >/dev/null || { echo "ERROR: zip not found" >&2; exit 1; }

run() {
    if [ "${DRY_RUN}" = true ]; then
        echo "Would execute: $*"
    else
        "$@"
    fi
}

echo "Publishing ${PROJECT} → ${OUT_DIR}"
run rm -rf "${OUT_DIR}" "${ZIP_PATH}"
run "${DOTNET}" publish "${PROJECT}" -c Release -r win-x64 --self-contained false -o "${OUT_DIR}"

# appsettings.json ships with empty values; real settings come from install.ps1 (service environment).
echo "Zipping → ${ZIP_PATH}"
if [ "${DRY_RUN}" = true ]; then
    echo "Would execute: (cd ${OUT_DIR} && zip -qr ${ZIP_PATH} .)"
else
    (cd "${OUT_DIR}" && zip -qr "${ZIP_PATH}" .)
    echo "Done: $(du -h "${ZIP_PATH}" | cut -f1) ${ZIP_PATH}"
    command -v shasum >/dev/null && shasum -a 256 "${ZIP_PATH}"
fi
