#!/bin/sh
set -eu

uv_version="0.12.3"
python_version="3.12.13"
officecli_version="1.0.143"
officecli_minimum="1.0.137"
release_root="https://github.com/karthiknambiar/csm-skills-framework/releases"
dry_run=0
assume_yes=0
codex_only=0
claude_only=0
gemini_only=0
requested_version=""
manifest_path=""
data_root=""
platform=""
staging_directory=""

fail() {
    printf '%s\n' "CSAF installer failed: $1" >&2
    exit 2
}

usage() {
    printf '%s\n' "Usage: install.sh [--dry-run] [--yes] [--version X.Y.Z] [--manifest FILE] [--data-root DIR] [--codex-only|--claude-only|--gemini-only]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1; shift ;;
        --yes) assume_yes=1; shift ;;
        --codex-only) codex_only=1; shift ;;
        --claude-only) claude_only=1; shift ;;
        --gemini-only) gemini_only=1; shift ;;
        --version|--manifest|--data-root|--platform)
            [ "$#" -ge 2 ] || fail "missing value for $1"
            case "$1" in
                --version) requested_version=$2 ;;
                --manifest) manifest_path=$2 ;;
                --data-root) data_root=$2 ;;
                --platform) platform=$2 ;;
            esac
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown option" ;;
    esac
done

[ $((codex_only + claude_only + gemini_only)) -le 1 ] || fail "choose only one assistant override"

if [ -z "$platform" ]; then
    kernel=$(uname -s 2>/dev/null) || fail "platform detection failed"
    machine=$(uname -m 2>/dev/null) || fail "platform detection failed"
    case "$kernel" in
        Darwin) system="macos" ;;
        Linux) system="linux" ;;
        *) fail "macOS or Linux is required" ;;
    esac
    case "$machine" in
        x86_64|amd64) architecture="x64" ;;
        arm64|aarch64) architecture="arm64" ;;
        *) fail "x64 or arm64 is required" ;;
    esac
    platform="$system-$architecture"
fi
case "$platform" in
    macos-x64|macos-arm64|linux-x64|linux-arm64) ;;
    *) fail "unsupported platform" ;;
esac

if [ -z "$data_root" ]; then
    case "$platform" in
        macos-*) data_root=${HOME:?HOME is unavailable}/Library/Application\ Support/CSAF ;;
        linux-*) data_root=${XDG_DATA_HOME:-${HOME:?HOME is unavailable}/.local/share}/csaf ;;
    esac
fi
case "$data_root" in
    /*) ;;
    *) fail "data root must be absolute" ;;
esac
[ "$data_root" != "/" ] || fail "data root must not be a filesystem root"

ensure_private_data_root() {
    component=$data_root
    while :; do
        [ -L "$component" ] && fail "CSAF data root contains a symbolic link"
        if [ -e "$component" ] && [ ! -d "$component" ]; then
            fail "CSAF data root must be a directory"
        fi
        [ "$component" = "/" ] && break
        component=$(dirname "$component") || fail "CSAF data root is invalid"
    done
    mkdir -p "$data_root" || fail "private data root could not be created"
    [ ! -L "$data_root" ] || fail "CSAF data root contains a symbolic link"
    [ -d "$data_root" ] || fail "CSAF data root must be a directory"
    chmod 700 "$data_root" || fail "private data root permissions could not be enforced"
}

ensure_private_directory() {
    controlled_directory=$1
    controlled_parent=$(dirname "$controlled_directory") ||
        fail "installer-controlled directory path is invalid"
    [ -d "$controlled_parent" ] && [ ! -L "$controlled_parent" ] ||
        fail "installer-controlled parent is unsafe"
    if [ -e "$controlled_directory" ] || [ -L "$controlled_directory" ]; then
        [ -d "$controlled_directory" ] && [ ! -L "$controlled_directory" ] ||
            fail "installer-controlled directory is unsafe"
    else
        mkdir "$controlled_directory" ||
            fail "installer-controlled directory could not be created"
    fi
    [ -d "$controlled_directory" ] && [ ! -L "$controlled_directory" ] ||
        fail "installer-controlled directory is unsafe"
    chmod 700 "$controlled_directory" ||
        fail "installer-controlled directory permissions could not be enforced"
}

safe_file_target() {
    controlled_file=$1
    allow_existing=$2
    controlled_parent=$(dirname "$controlled_file") ||
        fail "installer-controlled file path is invalid"
    [ -d "$controlled_parent" ] && [ ! -L "$controlled_parent" ] ||
        fail "installer-controlled file parent is unsafe"
    [ ! -L "$controlled_file" ] ||
        fail "installer-controlled file target is unsafe"
    if [ -e "$controlled_file" ]; then
        [ "$allow_existing" -eq 1 ] && [ -f "$controlled_file" ] ||
            fail "installer-controlled file target is unsafe"
    fi
}

case "$requested_version" in
    "") ;;
    *[!0-9.]*|.*|*..*|*.) fail "version must use X.Y.Z" ;;
    *)
        old_ifs=$IFS
        IFS=.
        set -- $requested_version
        IFS=$old_ifs
        [ "$#" -eq 3 ] || fail "version must use X.Y.Z"
        ;;
esac

if [ -n "$manifest_path" ]; then
    manifest_source=$manifest_path
elif [ -n "$requested_version" ]; then
    manifest_source="$release_root/download/v$requested_version/csaf-release-manifest.json"
else
    manifest_source="$release_root/latest/download/csaf-release-manifest.json"
fi

if [ -f "$manifest_source" ]; then
    manifest_bytes=$(wc -c < "$manifest_source" | tr -d ' ')
    [ "$manifest_bytes" -le 1048576 ] || fail "release manifest is too large"
    manifest_content=$(cat "$manifest_source") || fail "release manifest could not be read"
else
    case "$manifest_source" in https://*) ;; *) fail "manifest must be a local file or HTTPS URL" ;; esac
    [ "${CSAF_INSTALLER_NETWORK_FORBIDDEN:-0}" != "1" ] || fail "network access is disabled"
    command -v curl >/dev/null 2>&1 || fail "curl is required"
    manifest_content=$(curl --proto '=https' --proto-redir '=https' --tlsv1.2 --location --fail --silent --show-error --max-time 30 --max-filesize 1048576 "$manifest_source") || fail "release manifest could not be read"
    manifest_bytes=$(printf '%s' "$manifest_content" | wc -c | tr -d ' ')
    [ "$manifest_bytes" -le 1048576 ] || fail "release manifest is too large"
fi

command -v awk >/dev/null 2>&1 || fail "POSIX awk is required"
manifest_fields=$(printf '%s\n' "$manifest_content" | awk \
    -v selected_platform="$platform" \
    -v required_office_version="$officecli_version" \
    -v required_office_minimum="$officecli_minimum" '
function invalid() {
    failed = 1
    exit 3
}
function whitespace(    character) {
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character !~ /[ \t\r\n]/) {
            return
        }
        position += 1
    }
}
function json_string(    character, result) {
    if (substr(document, position, 1) != "\"") {
        invalid()
    }
    position += 1
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character == "\"") {
            position += 1
            return result
        }
        if (character == "\\" || character ~ /[[:cntrl:]]/) {
            invalid()
        }
        result = result character
        position += 1
    }
    invalid()
}
function json_number(    start, character, number) {
    start = position
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character !~ /[0-9eE+.-]/) {
            break
        }
        position += 1
    }
    number = substr(document, start, position - start)
    if (number !~ /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/) {
        invalid()
    }
    return number
}
function parse_array(path,    array_index, character) {
    node_type[path] = "array"
    position += 1
    whitespace()
    if (substr(document, position, 1) == "]") {
        position += 1
        return
    }
    array_index = 0
    while (1) {
        parse_value(path "/" array_index)
        array_index += 1
        whitespace()
        character = substr(document, position, 1)
        if (character == "]") {
            position += 1
            return
        }
        if (character != ",") {
            invalid()
        }
        position += 1
        whitespace()
    }
}
function parse_object(path,    key, marker, character) {
    node_type[path] = "object"
    position += 1
    whitespace()
    if (substr(document, position, 1) == "}") {
        position += 1
        return
    }
    while (1) {
        key = json_string()
        marker = path SUBSEP key
        if (object_key[marker]) {
            invalid()
        }
        object_key[marker] = 1
        object_count[path] += 1
        whitespace()
        if (substr(document, position, 1) != ":") {
            invalid()
        }
        position += 1
        parse_value(path "/" key)
        whitespace()
        character = substr(document, position, 1)
        if (character == "}") {
            position += 1
            return
        }
        if (character != ",") {
            invalid()
        }
        position += 1
        whitespace()
    }
}
function parse_value(path,    character, literal) {
    whitespace()
    character = substr(document, position, 1)
    if (character == "{") {
        parse_object(path)
        return
    }
    if (character == "[") {
        parse_array(path)
        return
    }
    if (character == "\"") {
        node_type[path] = "string"
        node_value[path] = json_string()
        return
    }
    if (character == "-" || character ~ /[0-9]/) {
        node_type[path] = "number"
        node_value[path] = json_number()
        return
    }
    literal = substr(document, position, 4)
    if (literal == "true" || substr(document, position, 5) == "false") {
        node_type[path] = "boolean"
        if (literal == "true") {
            node_value[path] = "true"
            position += 4
        } else {
            node_value[path] = "false"
            position += 5
        }
        return
    }
    if (literal == "null") {
        node_type[path] = "null"
        position += 4
        return
    }
    invalid()
}
function exact_object(path, expected,    names, count, item_index, marker) {
    if (node_type[path] != "object") {
        invalid()
    }
    count = split(expected, names, " ")
    if (object_count[path] != count) {
        invalid()
    }
    for (item_index = 1; item_index <= count; item_index += 1) {
        marker = path SUBSEP names[item_index]
        if (!object_key[marker]) {
            invalid()
        }
    }
}
function valid_asset(path,    url, digest, size) {
    exact_object(path, "url sha256 size")
    if (node_type[path "/url"] != "string" ||
        node_type[path "/sha256"] != "string" ||
        node_type[path "/size"] != "number") {
        invalid()
    }
    url = node_value[path "/url"]
    digest = node_value[path "/sha256"]
    size = node_value[path "/size"]
    if (url !~ /^https:\/\// || index(url, "/main/") != 0 ||
        length(digest) != 64 || digest ~ /[^0-9a-f]/ ||
        size !~ /^[1-9][0-9]*$/) {
        invalid()
    }
}
function valid_platform_assets(path,    platforms, count, item_index) {
    platforms = "windows-x64 windows-arm64 macos-x64 macos-arm64 linux-x64 linux-arm64"
    exact_object(path, platforms)
    count = split(platforms, platform_name, " ")
    for (item_index = 1; item_index <= count; item_index += 1) {
        valid_asset(path "/" platform_name[item_index])
    }
}
{
    document = document $0 "\n"
}
END {
    if (failed) {
        exit 3
    }
    document_length = length(document)
    position = 1
    parse_value("$")
    whitespace()
    if (position <= document_length) {
        invalid()
    }
    exact_object("$", "schema_version version runtime codex_skill claude_plugin officecli")
    if (node_type["$/schema_version"] != "number" ||
        node_value["$/schema_version"] != "1" ||
        node_type["$/version"] != "string" ||
        node_value["$/version"] !~ /^[0-9]+\.[0-9]+\.[0-9]+$/) {
        invalid()
    }
    valid_platform_assets("$/runtime")
    valid_asset("$/codex_skill")
    valid_asset("$/claude_plugin")
    exact_object("$/officecli", "version minimum_version assets")
    if (node_type["$/officecli/version"] != "string" ||
        node_value["$/officecli/version"] != required_office_version ||
        node_type["$/officecli/minimum_version"] != "string" ||
        node_value["$/officecli/minimum_version"] != required_office_minimum) {
        invalid()
    }
    valid_platform_assets("$/officecli/assets")
    if (!object_key["$/runtime" SUBSEP selected_platform]) {
        invalid()
    }
    print node_value["$/version"]
    print node_value["$/runtime/" selected_platform "/url"]
    print node_value["$/runtime/" selected_platform "/sha256"]
    print node_value["$/runtime/" selected_platform "/size"]
}
') || fail "release manifest is invalid"
release_version=$(printf '%s\n' "$manifest_fields" | sed -n '1p')
early_runtime_url=$(printf '%s\n' "$manifest_fields" | sed -n '2p')
early_runtime_sha=$(printf '%s\n' "$manifest_fields" | sed -n '3p')
early_runtime_size=$(printf '%s\n' "$manifest_fields" | sed -n '4p')
[ -n "$release_version" ] && [ -n "$early_runtime_url" ] &&
    [ -n "$early_runtime_sha" ] && [ -n "$early_runtime_size" ] ||
    fail "release manifest is invalid"
[ -z "$requested_version" ] || [ "$release_version" = "$requested_version" ] ||
    fail "requested version does not match manifest"

codex_skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
codex_selected=0
claude_selected=0
gemini_selected=0
if [ "$codex_only" -eq 1 ]; then
    targets="codex"
    codex_selected=1
elif [ "$claude_only" -eq 1 ]; then
    targets="claude"
    claude_selected=1
elif [ "$gemini_only" -eq 1 ]; then
    targets="gemini"
    gemini_selected=1
else
    targets=""
    if [ -n "${CODEX_HOME:-}" ] || [ -d "$codex_skill_root" ] ||
        command -v codex >/dev/null 2>&1; then
        targets="codex"
        codex_selected=1
    fi
    if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
        claude_selected=1
        if [ -n "$targets" ]; then targets="$targets, claude"; else targets="claude"; fi
    fi
    if [ -d "$HOME/.gemini" ] || command -v gemini >/dev/null 2>&1; then
        gemini_selected=1
        if [ -n "$targets" ]; then targets="$targets, gemini"; else targets="gemini"; fi
    fi
    [ -n "$targets" ] || targets="runtime only (none detected)"
fi

uv_path="$data_root/bin/uv"
python_root="$data_root/python"
uv_cache="$data_root/cache/uv"
officecli_path="$data_root/officecli/$officecli_version/officecli"
codex_adapter_path="$codex_skill_root/csaf"
claude_adapter_path="$data_root/adapters/claude"
gemini_adapter_path="$HOME/.gemini/skills/csaf"

printf '%s\n' "CSAF $release_version installation plan"
printf '%s\n' "Platform: $platform"
printf '%s\n' "Targets: $targets"
printf '%s\n' "Data root: $data_root"
printf '%s\n' "Private uv 0.12.3: $uv_path"
printf '%s\n' "Private Python 3.12.13: $python_root"
printf '%s\n' "Mandatory OfficeCLI 1.0.143: $officecli_path"
[ "$codex_selected" -eq 0 ] ||
    printf '%s\n' "Codex adapter destination: $codex_adapter_path"
[ "$claude_selected" -eq 0 ] ||
    printf '%s\n' "Claude adapter destination: $claude_adapter_path"
[ "$gemini_selected" -eq 0 ] ||
    printf '%s\n' "Gemini adapter destination: $gemini_adapter_path"
printf '%s\n' "OfficeCLI is mandatory because QBR PowerPoint and Word generation cannot work without it."
printf '%s\n' "CSAF and OfficeCLI run locally with no API key or hosted AI service."
printf '%s\n' "Release source: $manifest_source"
printf '%s\n' "Network: verified HTTPS release assets only; normal installed operation is offline."

if [ "$dry_run" -eq 1 ]; then
    printf '%s\n' "Dry run complete; no downloads or filesystem changes were made."
    exit 0
fi
if [ "$assume_yes" -ne 1 ]; then
    printf '%s' "Install CSAF and mandatory OfficeCLI into every selected assistant? [y/N] "
    IFS= read -r answer || answer=""
    case "$answer" in y|Y|yes|YES|Yes) ;; *) fail "installation was declined" ;; esac
fi

cleanup() {
    if [ -n "$staging_directory" ] && [ -d "$staging_directory" ] &&
        [ ! -L "$staging_directory" ]; then
        case "$staging_directory" in
            "$data_root"/staging/bootstrap-*) rm -rf -- "$staging_directory" ;;
            *) printf '%s\n' "CSAF installer refused unsafe staging cleanup" >&2 ;;
        esac
    fi
}
trap cleanup EXIT HUP INT TERM

umask 077
ensure_private_data_root
ensure_private_directory "$data_root/staging"
ensure_private_directory "$data_root/bin"
ensure_private_directory "$data_root/python"
ensure_private_directory "$data_root/cache"
ensure_private_directory "$data_root/cache/uv"
staging_directory=$(mktemp -d "$data_root/staging/bootstrap-XXXXXXXX") ||
    fail "private staging directory could not be created"
[ -d "$staging_directory" ] && [ ! -L "$staging_directory" ] ||
    fail "private staging directory is unsafe"
chmod 700 "$staging_directory" ||
    fail "private staging directory permissions could not be enforced"
bootstrap_log="$staging_directory/bootstrap.log"
manifest_file="$staging_directory/csaf-release-manifest.json"
safe_file_target "$manifest_file" 0
printf '%s\n' "$manifest_content" > "$manifest_file" ||
    fail "release manifest could not be staged"
safe_file_target "$manifest_file" 1

case "$platform" in
    linux-arm64)
        uv_url="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-unknown-linux-gnu.tar.gz"
        uv_sha="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
        uv_size=20423730
        ;;
    linux-x64)
        uv_url="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-unknown-linux-gnu.tar.gz"
        uv_sha="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
        uv_size=21721441
        ;;
    macos-arm64)
        uv_url="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz"
        uv_sha="546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
        uv_size=17686637
        ;;
    macos-x64)
        uv_url="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-apple-darwin.tar.gz"
        uv_sha="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
        uv_size=19547702
        ;;
esac

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | sed 's/[[:space:]].*$//'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | sed 's/[[:space:]].*$//'
    else
        fail "sha256sum or shasum is required"
    fi
}

download_verified() {
    source_url=$1
    expected_sha=$2
    expected_size=$3
    destination=$4
    [ "${CSAF_INSTALLER_NETWORK_FORBIDDEN:-0}" != "1" ] || fail "network access is disabled"
    safe_file_target "$destination" 0
    if ! curl --proto '=https' --proto-redir '=https' --tlsv1.2 --location --fail --silent --show-error --max-time 120 --max-filesize "$expected_size" --output "$destination" "$source_url" 2> "$bootstrap_log"; then
        fail "verified asset download failed"
    fi
    actual_size=$(wc -c < "$destination" | tr -d ' ')
    [ "$actual_size" = "$expected_size" ] || fail "downloaded asset size did not match"
    actual_sha=$(sha256_file "$destination")
    [ "$actual_sha" = "$expected_sha" ] || fail "downloaded asset checksum did not match"
}

invoke_checked() {
    if ! "$@" >> "$bootstrap_log" 2>&1; then
        fail "private bootstrap command failed"
    fi
}

uv_archive="$staging_directory/uv.tar.gz"
download_verified "$uv_url" "$uv_sha" "$uv_size" "$uv_archive"
uv_extracted="$staging_directory/uv-extracted"
ensure_private_directory "$uv_extracted"
if ! tar -xzf "$uv_archive" -C "$uv_extracted" >> "$bootstrap_log" 2>&1; then
    fail "verified uv archive could not be extracted"
fi
uv_candidate=""
for candidate in "$uv_extracted"/uv "$uv_extracted"/*/uv; do
    if [ -f "$candidate" ]; then uv_candidate=$candidate; break; fi
done
[ -n "$uv_candidate" ] || fail "verified uv archive was incomplete"
safe_file_target "$uv_path" 1
install -m 700 "$uv_candidate" "$uv_path" || fail "private uv could not be installed"
safe_file_target "$uv_path" 1

export UV_UNMANAGED_INSTALL="$data_root/bin"
export UV_PYTHON_INSTALL_DIR="$python_root"
export UV_CACHE_DIR="$uv_cache"
export UV_NO_CONFIG=1
unset UV_OFFLINE || true
invoke_checked "$uv_path" python install "$python_version"
python_executable=$("$uv_path" python find --python-preference only-managed "$python_version" 2>> "$bootstrap_log") || fail "private Python installation was not found"
case "$python_executable" in "$python_root"/*) ;; *) fail "private Python resolved outside the CSAF data root" ;; esac
[ -f "$python_executable" ] || fail "private Python installation was not found"

asset_fields=$("$python_executable" -I -S -c '
import json, pathlib, re, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
platform = sys.argv[2]
platforms = {"windows-x64", "windows-arm64", "macos-x64", "macos-arm64", "linux-x64", "linux-arm64"}
def require(condition):
    if not condition:
        raise ValueError("release manifest is invalid")
def asset(value):
    return set(value) == {"url", "sha256", "size"} and isinstance(value["url"], str) and value["url"].startswith("https://") and "/main/" not in value["url"] and isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) and type(value["size"]) is int and value["size"] > 0
require(set(manifest) == {"schema_version", "version", "runtime", "codex_skill", "claude_plugin", "officecli"})
require(manifest["schema_version"] == 1 and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", manifest["version"]))
require(set(manifest["runtime"]) == platforms and all(asset(item) for item in manifest["runtime"].values()))
require(asset(manifest["codex_skill"]) and asset(manifest["claude_plugin"]))
office = manifest["officecli"]
require(set(office) == {"version", "minimum_version", "assets"})
require(office["version"] == "1.0.143" and office["minimum_version"] == "1.0.137")
require(set(office["assets"]) == platforms and all(asset(item) for item in office["assets"].values()))
selected = manifest["runtime"][platform]
print(selected["url"]); print(selected["sha256"]); print(selected["size"])
' "$manifest_file" "$platform" 2>> "$bootstrap_log") || fail "release manifest is invalid"
runtime_url=$(printf '%s\n' "$asset_fields" | sed -n '1p')
runtime_sha=$(printf '%s\n' "$asset_fields" | sed -n '2p')
runtime_size=$(printf '%s\n' "$asset_fields" | sed -n '3p')
runtime_archive="$staging_directory/runtime-bundle.zip"
download_verified "$runtime_url" "$runtime_sha" "$runtime_size" "$runtime_archive"

runtime_bundle="$staging_directory/runtime-bundle"
[ ! -e "$runtime_bundle" ] && [ ! -L "$runtime_bundle" ] ||
    fail "runtime bundle staging path is unsafe"
# Validates runtime-bundle.json; kept inline so bootstrap has no unverified helper dependency.
invoke_checked "$python_executable" -I -S -c "import base64; exec(base64.b64decode('aW1wb3J0IGhhc2hsaWIsIGpzb24sIHBhdGhsaWIsIHJlLCBzdGF0LCBzeXMsIHVuaWNvZGVkYXRhLCB6aXBmaWxlCgpkZWYgcmVxdWlyZShjb25kaXRpb24pOgogICAgaWYgbm90IGNvbmRpdGlvbjoKICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJydW50aW1lIGJ1bmRsZSBpcyBpbnZhbGlkIikKCmFyY2hpdmUgPSBwYXRobGliLlBhdGgoc3lzLmFyZ3ZbMV0pCmRlc3RpbmF0aW9uID0gcGF0aGxpYi5QYXRoKHN5cy5hcmd2WzJdKQpwbGF0Zm9ybSA9IHN5cy5hcmd2WzNdCmV4cGVjdGVkX3ZlcnNpb24gPSBzeXMuYXJndls0XQpwbGF0Zm9ybXMgPSB7IndpbmRvd3MteDY0IiwgIndpbmRvd3MtYXJtNjQiLCAibWFjb3MteDY0IiwgIm1hY29zLWFybTY0IiwgImxpbnV4LXg2NCIsICJsaW51eC1hcm02NCJ9CnJlcXVpcmUocGxhdGZvcm0gaW4gcGxhdGZvcm1zKQpyZXF1aXJlKHJlLmZ1bGxtYXRjaChyIlswLTldK1wuWzAtOV0rXC5bMC05XSsiLCBleHBlY3RlZF92ZXJzaW9uKSBpcyBub3QgTm9uZSkKcmVxdWlyZShub3QgZGVzdGluYXRpb24uZXhpc3RzKCkpCndpdGggYXJjaGl2ZS5vcGVuKCJyYiIpIGFzIHNvdXJjZSwgemlwZmlsZS5aaXBGaWxlKHNvdXJjZSkgYXMgYnVuZGxlOgogICAgaW5mb3MgPSBidW5kbGUuaW5mb2xpc3QoKQogICAgcmVxdWlyZShsZW4oaW5mb3MpIDw9IDI1NikKICAgIG5hbWVzID0gW2l0ZW0uZmlsZW5hbWUgZm9yIGl0ZW0gaW4gaW5mb3NdCiAgICByZXF1aXJlKGxlbihzZXQobmFtZXMpKSA9PSBsZW4oaW5mb3MpKQogICAgZm9sZGVkID0ge3VuaWNvZGVkYXRhLm5vcm1hbGl6ZSgiTkZDIiwgbmFtZSkuY2FzZWZvbGQoKSBmb3IgbmFtZSBpbiBuYW1lc30KICAgIHJlcXVpcmUobGVuKGZvbGRlZCkgPT0gbGVuKGluZm9zKSkKICAgIHJlcXVpcmUoc3VtKGl0ZW0uZmlsZV9zaXplIGZvciBpdGVtIGluIGluZm9zKSA8PSAxMDczNzQxODI0KQogICAgZm9yIGl0ZW0gaW4gaW5mb3M6CiAgICAgICAgbmFtZSA9IHBhdGhsaWIuUHVyZVBvc2l4UGF0aChpdGVtLmZpbGVuYW1lKQogICAgICAgIHJlcXVpcmUobGVuKGl0ZW0uZmlsZW5hbWUuZW5jb2RlKCJ1dGYtOCIpKSA8PSA0MDk2KQogICAgICAgIHJlcXVpcmUobm90IGl0ZW0uaXNfZGlyKCkgYW5kIG5vdCBuYW1lLmlzX2Fic29sdXRlKCkgYW5kICIuLiIgbm90IGluIG5hbWUucGFydHMpCiAgICAgICAgcmVxdWlyZShpdGVtLmZpbGVfc2l6ZSA8PSAyNjg0MzU0NTYpCiAgICAgICAgcmVxdWlyZShzdGF0LlNfSUZNVChpdGVtLmV4dGVybmFsX2F0dHIgPj4gMTYpIGluICgwLCBzdGF0LlNfSUZSRUcpKQogICAgbmFtZV9zZXQgPSBzZXQobmFtZXMpCiAgICByZXF1aXJlKCJydW50aW1lLWJ1bmRsZS5qc29uIiBpbiBuYW1lX3NldCkKICAgIG1hbmlmZXN0X2luZm8gPSBidW5kbGUuZ2V0aW5mbygicnVudGltZS1idW5kbGUuanNvbiIpCiAgICByZXF1aXJlKG1hbmlmZXN0X2luZm8uZmlsZV9zaXplIDw9IDEwNDg1NzYpCiAgICBtYW5pZmVzdCA9IGpzb24ubG9hZHMoYnVuZGxlLnJlYWQobWFuaWZlc3RfaW5mbykuZGVjb2RlKCJ1dGYtOCIpKQogICAgcmVxdWlyZSh0eXBlKG1hbmlmZXN0KSBpcyBkaWN0KQogICAgcmVxdWlyZShzZXQobWFuaWZlc3QpID09IHsic2NoZW1hX3ZlcnNpb24iLCAidmVyc2lvbiIsICJwbGF0Zm9ybSIsICJmaWxlcyJ9KQogICAgcmVxdWlyZSh0eXBlKG1hbmlmZXN0WyJzY2hlbWFfdmVyc2lvbiJdKSBpcyBpbnQgYW5kIG1hbmlmZXN0WyJzY2hlbWFfdmVyc2lvbiJdID09IDEpCiAgICByZXF1aXJlKHR5cGUobWFuaWZlc3RbInZlcnNpb24iXSkgaXMgc3RyIGFuZCBtYW5pZmVzdFsidmVyc2lvbiJdID09IGV4cGVjdGVkX3ZlcnNpb24pCiAgICByZXF1aXJlKG1hbmlmZXN0WyJwbGF0Zm9ybSJdID09IHBsYXRmb3JtKQogICAgZmlsZXMgPSBtYW5pZmVzdFsiZmlsZXMiXQogICAgcmVxdWlyZSh0eXBlKGZpbGVzKSBpcyBkaWN0KQogICAgcmVxdWlyZShzZXQoZmlsZXMpID09IG5hbWVfc2V0IC0geyJydW50aW1lLWJ1bmRsZS5qc29uIn0pCiAgICBydW50aW1lX3doZWVsID0gZiJjc2FmLXtleHBlY3RlZF92ZXJzaW9ufS1weTMtbm9uZS1hbnkud2hsIgogICAgcmVxdWlyZShydW50aW1lX3doZWVsIGluIGZpbGVzIGFuZCAicmVxdWlyZW1lbnRzLmxvY2siIGluIGZpbGVzKQogICAgd2hlZWxfbmFtZXMgPSBzb3J0ZWQobmFtZSBmb3IgbmFtZSBpbiBmaWxlcyBpZiBuYW1lLnN0YXJ0c3dpdGgoIndoZWVsaG91c2UvIikgYW5kIG5hbWUuZW5kc3dpdGgoIi53aGwiKSkKICAgIHJlcXVpcmUoYm9vbCh3aGVlbF9uYW1lcykpCiAgICByZXF1aXJlKHNldChmaWxlcykgPT0ge3J1bnRpbWVfd2hlZWwsICJyZXF1aXJlbWVudHMubG9jayIsICp3aGVlbF9uYW1lc30pCiAgICBkaWdlc3RzID0ge30KICAgIGZvciBuYW1lLCBleHBlY3RlZCBpbiBmaWxlcy5pdGVtcygpOgogICAgICAgIHJlcXVpcmUodHlwZShleHBlY3RlZCkgaXMgZGljdCBhbmQgc2V0KGV4cGVjdGVkKSA9PSB7InNoYTI1NiIsICJzaXplIn0pCiAgICAgICAgcmVxdWlyZSh0eXBlKGV4cGVjdGVkWyJzaGEyNTYiXSkgaXMgc3RyIGFuZCByZS5mdWxsbWF0Y2gociJbMC05YS1mXXs2NH0iLCBleHBlY3RlZFsic2hhMjU2Il0pIGlzIG5vdCBOb25lKQogICAgICAgIHJlcXVpcmUodHlwZShleHBlY3RlZFsic2l6ZSJdKSBpcyBpbnQgYW5kIGV4cGVjdGVkWyJzaXplIl0gPiAwKQogICAgICAgIGluZm8gPSBidW5kbGUuZ2V0aW5mbyhuYW1lKQogICAgICAgIHJlcXVpcmUoaW5mby5maWxlX3NpemUgPT0gZXhwZWN0ZWRbInNpemUiXSkKICAgICAgICBkaWdlc3QgPSBoYXNobGliLnNoYTI1NigpCiAgICAgICAgd2l0aCBidW5kbGUub3BlbihpbmZvKSBhcyBpbmNvbWluZzoKICAgICAgICAgICAgd2hpbGUgY2h1bmsgOj0gaW5jb21pbmcucmVhZCgxMDQ4NTc2KToKICAgICAgICAgICAgICAgIGRpZ2VzdC51cGRhdGUoY2h1bmspCiAgICAgICAgcmVxdWlyZShkaWdlc3QuaGV4ZGlnZXN0KCkgPT0gZXhwZWN0ZWRbInNoYTI1NiJdKQogICAgICAgIGRpZ2VzdHNbbmFtZV0gPSBkaWdlc3QuaGV4ZGlnZXN0KCkKICAgIGxvY2tfaW5mbyA9IGJ1bmRsZS5nZXRpbmZvKCJyZXF1aXJlbWVudHMubG9jayIpCiAgICByZXF1aXJlKGxvY2tfaW5mby5maWxlX3NpemUgPD0gMTA0ODU3NikKICAgIGxvY2sgPSBidW5kbGUucmVhZChsb2NrX2luZm8pLmRlY29kZSgidXRmLTgiKS5zcGxpdGxpbmVzKCkKICAgIHJlcXVpcmUoYm9vbChsb2NrKSBhbmQgYWxsKGxpbmUgYW5kIGxpbmUgPT0gbGluZS5zdHJpcCgpIGZvciBsaW5lIGluIGxvY2spKQogICAgcmVxdWlyZShsb2NrWzBdID09IGYiLi97cnVudGltZV93aGVlbH0gLS1oYXNoPXNoYTI1Njp7ZGlnZXN0c1tydW50aW1lX3doZWVsXX0iKQogICAgZXhwZWN0ZWQgPSB7ZiJ7cGF0aGxpYi5QdXJlUG9zaXhQYXRoKG5hbWUpLm5hbWUuc3BsaXQoJy0nKVswXS5yZXBsYWNlKCdfJywgJy0nKX09PXtwYXRobGliLlB1cmVQb3NpeFBhdGgobmFtZSkubmFtZS5zcGxpdCgnLScpWzFdfSAtLWhhc2g9c2hhMjU2OntkaWdlc3RzW25hbWVdfSIgZm9yIG5hbWUgaW4gd2hlZWxfbmFtZXN9CiAgICByZXF1aXJlKHNldChsb2NrWzE6XSkgPT0gZXhwZWN0ZWQgYW5kIGxlbihsb2NrWzE6XSkgPT0gbGVuKGV4cGVjdGVkKSkKICAgIGRlc3RpbmF0aW9uLm1rZGlyKCkKICAgIGZvciBuYW1lLCBleHBlY3RlZF9maWxlIGluIGZpbGVzLml0ZW1zKCk6CiAgICAgICAgaW5mbyA9IGJ1bmRsZS5nZXRpbmZvKG5hbWUpCiAgICAgICAgdGFyZ2V0ID0gZGVzdGluYXRpb24gLyBwYXRobGliLlB1cmVQb3NpeFBhdGgobmFtZSkKICAgICAgICB0YXJnZXQucGFyZW50Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICBkaWdlc3QgPSBoYXNobGliLnNoYTI1NigpCiAgICAgICAgd2l0aCBidW5kbGUub3BlbihpbmZvKSBhcyBpbmNvbWluZywgdGFyZ2V0Lm9wZW4oInhiIikgYXMgb3V0Z29pbmc6CiAgICAgICAgICAgIHdoaWxlIGNodW5rIDo9IGluY29taW5nLnJlYWQoMTA0ODU3Nik6CiAgICAgICAgICAgICAgICBkaWdlc3QudXBkYXRlKGNodW5rKQogICAgICAgICAgICAgICAgb3V0Z29pbmcud3JpdGUoY2h1bmspCiAgICAgICAgcmVxdWlyZShkaWdlc3QuaGV4ZGlnZXN0KCkgPT0gZXhwZWN0ZWRfZmlsZVsic2hhMjU2Il0p'))" "$runtime_archive" "$runtime_bundle" "$platform" "$release_version"

[ -d "$runtime_bundle" ] && [ ! -L "$runtime_bundle" ] ||
    fail "runtime bundle staging path is unsafe"
export UV_OFFLINE=1
invoke_checked "$uv_path" pip install --python "$python_executable" --offline --no-config --no-index --require-hashes --find-links "$runtime_bundle/wheelhouse" --requirement "$runtime_bundle/requirements.lock"

export CSAF_DATA_ROOT="$data_root"
if [ "$codex_only" -eq 1 ]; then
    invoke_checked "$python_executable" -m csaf.setup.cli install --manifest "$manifest_file" --yes --codex-only
elif [ "$claude_only" -eq 1 ]; then
    invoke_checked "$python_executable" -m csaf.setup.cli install --manifest "$manifest_file" --yes --claude-only
elif [ "$gemini_only" -eq 1 ]; then
    invoke_checked "$python_executable" -m csaf.setup.cli install --manifest "$manifest_file" --yes --gemini-only
else
    invoke_checked "$python_executable" -m csaf.setup.cli install --manifest "$manifest_file" --yes
fi
printf '%s\n' "CSAF installation is ready. Diagnose later with: csaf setup doctor"
