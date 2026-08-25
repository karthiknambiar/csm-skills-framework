#!/bin/sh
set -eu

bootstrap_required() {
    printf '%s\n' '{"status":"bootstrap_required","reason":"runtime_missing_or_unhealthy","next_action":"run_platform_bootstrap_after_explicit_consent","requires_consent":true,"installs":["CSAF","OfficeCLI"],"network":"verified tagged stable release assets over HTTPS","api_key_required":false,"hosted_ai":false,"bootstrap":{"url":"https://github.com/karthiknambiar/csm-skills-framework/releases/latest/download/install.sh","invocation":"sh <downloaded-install.sh>"}}' >&2
    exit 3
}

normalize_root() {
    normalized=$1
    case "$normalized" in /*) ;; *) bootstrap_required ;; esac
    while [ "$normalized" != "/" ] && [ "${normalized%/}" != "$normalized" ]; do
        normalized=${normalized%/}
    done
    [ "$normalized" != "/" ] || bootstrap_required
    printf '%s\n' "$normalized"
}

if [ -n "${CSAF_DATA_ROOT-}" ]; then
    data_root=$(normalize_root "$CSAF_DATA_ROOT")
else
    case "$(uname -s 2>/dev/null || printf unknown)" in
        Darwin)
            [ -n "${HOME-}" ] || bootstrap_required
            home_root=$(normalize_root "$HOME")
            data_root=$home_root/Library/Application\ Support/CSAF
            ;;
        *)
            if [ -n "${XDG_DATA_HOME-}" ]; then
                xdg_root=$(normalize_root "$XDG_DATA_HOME")
                data_root=$xdg_root/csaf
            else
                [ -n "${HOME-}" ] || bootstrap_required
                home_root=$(normalize_root "$HOME")
                data_root=$home_root/.local/share/csaf
            fi
            ;;
    esac
fi

assert_real_path_chain() {
    controlled=$1
    leaf_type=$2
    first=1
    while :; do
        [ -e "$controlled" ] && [ ! -L "$controlled" ] || bootstrap_required
        if [ "$first" -eq 1 ]; then
            case "$leaf_type" in
                directory) [ -d "$controlled" ] || bootstrap_required ;;
                file) [ -f "$controlled" ] || bootstrap_required ;;
                *) bootstrap_required ;;
            esac
            first=0
        fi
        [ "$controlled" = "/" ] && break
        parent=$(dirname "$controlled") || bootstrap_required
        [ "$parent" != "$controlled" ] || break
        controlled=$parent
    done
}

current_path=$data_root/current.json
state_path=$data_root/state.json
assert_real_path_chain "$data_root" directory
assert_real_path_chain "$current_path" file
assert_real_path_chain "$state_path" file
for metadata in "$current_path" "$state_path"; do
    [ "$(wc -c < "$metadata")" -le 1048576 ] || bootstrap_required
done
command -v awk >/dev/null 2>&1 || bootstrap_required

parse_metadata() {
    metadata_kind=$1
    metadata_file=$2
    awk -v metadata_kind="$metadata_kind" -v expected_root="$data_root" '
function invalid() { failed = 1; exit 3 }
function whitespace(    character) {
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character !~ /[ \t\r\n]/) return
        position += 1
    }
}
function json_string(    character, result) {
    if (substr(document, position, 1) != "\"") invalid()
    position += 1
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character == "\"") { position += 1; return result }
        if (character == "\\" || character ~ /[[:cntrl:]]/) invalid()
        result = result character
        position += 1
    }
    invalid()
}
function json_number(    start, character, number) {
    start = position
    while (position <= document_length) {
        character = substr(document, position, 1)
        if (character !~ /[0-9eE+.-]/) break
        position += 1
    }
    number = substr(document, start, position - start)
    if (number !~ /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/) invalid()
    return number
}
function parse_array(path,    array_index, character) {
    node_type[path] = "array"
    position += 1
    whitespace()
    if (substr(document, position, 1) == "]") { position += 1; return }
    array_index = 0
    while (1) {
        parse_value(path "/" array_index)
        array_count[path] += 1
        array_index += 1
        whitespace()
        character = substr(document, position, 1)
        if (character == "]") { position += 1; return }
        if (character != ",") invalid()
        position += 1
        whitespace()
    }
}
function parse_object(path,    key, marker, character) {
    node_type[path] = "object"
    position += 1
    whitespace()
    if (substr(document, position, 1) == "}") { position += 1; return }
    while (1) {
        key = json_string()
        marker = path SUBSEP key
        if (object_key[marker]) invalid()
        object_key[marker] = 1
        object_count[path] += 1
        whitespace()
        if (substr(document, position, 1) != ":") invalid()
        position += 1
        parse_value(path "/" key)
        whitespace()
        character = substr(document, position, 1)
        if (character == "}") { position += 1; return }
        if (character != ",") invalid()
        position += 1
        whitespace()
    }
}
function parse_value(path,    character, literal) {
    whitespace()
    character = substr(document, position, 1)
    if (character == "{") { parse_object(path); return }
    if (character == "[") { parse_array(path); return }
    if (character == "\"") {
        node_type[path] = "string"; node_value[path] = json_string(); return
    }
    if (character == "-" || character ~ /[0-9]/) {
        node_type[path] = "number"; node_value[path] = json_number(); return
    }
    literal = substr(document, position, 4)
    if (literal == "true" || substr(document, position, 5) == "false") {
        node_type[path] = "boolean"
        if (literal == "true") { node_value[path] = "true"; position += 4 }
        else { node_value[path] = "false"; position += 5 }
        return
    }
    if (literal == "null") { node_type[path] = "null"; position += 4; return }
    invalid()
}
function exact_object(path, expected,    names, count, item_index, marker) {
    if (node_type[path] != "object") invalid()
    count = split(expected, names, " ")
    if (object_count[path] != count) invalid()
    for (item_index = 1; item_index <= count; item_index += 1) {
        marker = path SUBSEP names[item_index]
        if (!object_key[marker]) invalid()
    }
}
function string_map(path,    marker) {
    if (node_type[path] != "object") invalid()
    for (marker in object_key) {
        if (index(marker, path SUBSEP) == 1) {
            split(marker, parts, SUBSEP)
            if (node_type[path "/" parts[2]] != "string") invalid()
        }
    }
}
function is_version(value) {
    return value ~ /^[0-9]+\.[0-9]+\.[0-9]+$/
}
function is_absolute_normalized(path) {
    return substr(path, 1, 1) == "/" && path != "/" &&
        path !~ /\/\// && path !~ /(^|\/)\.\.?($|\/)/ && path !~ /\/$/
}
{
    document = document $0 "\n"
}
END {
    if (failed) exit 3
    document_length = length(document)
    position = 1
    parse_value("$")
    whitespace()
    if (position <= document_length) invalid()
    if (metadata_kind == "current") {
        exact_object("$", "schema_version active_version runtime_path")
        if (node_type["$/schema_version"] != "number" ||
            node_value["$/schema_version"] != "1" ||
            node_type["$/active_version"] != "string" ||
            node_value["$/active_version"] !~ /^[0-9]+\.[0-9]+\.[0-9]+$/ ||
            node_type["$/runtime_path"] != "string") invalid()
        if (index(node_value["$/active_version"], "|") ||
            index(node_value["$/runtime_path"], "|")) invalid()
        print node_value["$/active_version"] "|" node_value["$/runtime_path"]
    } else if (metadata_kind == "state") {
        exact_object("$", "schema_version active_version installed_versions runtime_paths verified_checksums adapter_targets officecli_version officecli_path officecli_sha256 officecli_installed_by_csaf installed_at updated_at")
        if (node_type["$/schema_version"] != "number" ||
            node_value["$/schema_version"] != "1" ||
            node_type["$/active_version"] != "string" ||
            node_type["$/installed_versions"] != "array" ||
            node_type["$/officecli_version"] != "string" ||
            node_value["$/officecli_version"] !~ /^[0-9]+\.[0-9]+\.[0-9]+$/ ||
            node_type["$/officecli_path"] != "string" ||
            node_type["$/officecli_sha256"] != "string" ||
            length(node_value["$/officecli_sha256"]) != 64 ||
            node_value["$/officecli_sha256"] ~ /[^0-9a-f]/ ||
            node_type["$/officecli_installed_by_csaf"] != "boolean" ||
            node_type["$/installed_at"] != "string" ||
            node_type["$/updated_at"] != "string") invalid()
        string_map("$/runtime_paths")
        string_map("$/verified_checksums")
        string_map("$/adapter_targets")
        active = node_value["$/active_version"]
        installed = 0
        installed_count = array_count["$/installed_versions"]
        for (item = 0; item < installed_count; item += 1) {
            installed_value = node_value["$/installed_versions/" item]
            if (node_type["$/installed_versions/" item] != "string" ||
                !is_version(installed_value) || installed_version[installed_value]) invalid()
            installed_version[installed_value] = 1
            if (installed_value == active) installed = 1
        }
        if (!installed || object_count["$/runtime_paths"] != installed_count) invalid()
        for (marker in object_key) {
            if (index(marker, "$/runtime_paths" SUBSEP) == 1) {
                split(marker, parts, SUBSEP)
                map_key = parts[2]
                map_value = node_value["$/runtime_paths/" map_key]
                if (!is_version(map_key) || !installed_version[map_key] ||
                    !is_absolute_normalized(map_value) ||
                    map_value != expected_root "/versions/" map_key) invalid()
            }
        }
        for (version in installed_version) {
            if (!object_key["$/runtime_paths" SUBSEP version]) invalid()
        }
        for (marker in object_key) {
            if (index(marker, "$/verified_checksums" SUBSEP) == 1) {
                split(marker, parts, SUBSEP)
                checksum_key = parts[2]
                checksum_value = node_value["$/verified_checksums/" checksum_key]
                checksum_version = ""
                if (checksum_key ~ /^runtime:/) checksum_version = substr(checksum_key, 9)
                else if (checksum_key ~ /^runtime-content:/) checksum_version = substr(checksum_key, 17)
                else if (checksum_key ~ /^officecli:/) {
                    checksum_version = substr(checksum_key, 11)
                    if (checksum_version != node_value["$/officecli_version"]) invalid()
                } else if (checksum_key ~ /^adapter:(codex|claude):/) {
                    checksum_parts_count = split(checksum_key, checksum_parts, ":")
                    if (checksum_parts_count != 3) invalid()
                    checksum_version = checksum_parts[3]
                } else invalid()
                if (!is_version(checksum_version) ||
                    (checksum_key !~ /^officecli:/ && !installed_version[checksum_version]) ||
                    length(checksum_value) != 64 || checksum_value ~ /[^0-9a-f]/) invalid()
            }
        }
        for (marker in object_key) {
            if (index(marker, "$/adapter_targets" SUBSEP) == 1) {
                split(marker, parts, SUBSEP)
                adapter_key = parts[2]
                adapter_value = node_value["$/adapter_targets/" adapter_key]
                if (adapter_key !~ /^(codex|claude)$/ ||
                    !is_absolute_normalized(adapter_value)) invalid()
            }
        }
        if (!is_absolute_normalized(node_value["$/officecli_path"]) ||
            index(active, "|") || index(node_value["$/officecli_version"], "|") ||
            index(node_value["$/officecli_path"], "|") ||
            index(node_value["$/runtime_paths/" active], "|")) invalid()
        print active "|" node_value["$/officecli_version"] "|" node_value["$/officecli_path"] "|" node_value["$/runtime_paths/" active]
    } else invalid()
}
' "$metadata_file"
}

current_fields=$(parse_metadata current "$current_path") || bootstrap_required
active_version=${current_fields%%|*}
runtime_path=${current_fields#*|}
state_fields=$(parse_metadata state "$state_path") || bootstrap_required
state_version=${state_fields%%|*}
state_remainder=${state_fields#*|}
officecli_version=${state_remainder%%|*}
state_remainder=${state_remainder#*|}
officecli_path=${state_remainder%%|*}
recorded_runtime=${state_remainder#*|}
[ "$state_version" = "$active_version" ] || bootstrap_required

expected_runtime=$data_root/versions/$active_version
expected_officecli=$data_root/officecli/$officecli_version/officecli
[ "$runtime_path" = "$expected_runtime" ] || bootstrap_required
[ "$recorded_runtime" = "$runtime_path" ] || bootstrap_required
[ "$officecli_path" = "$expected_officecli" ] || bootstrap_required
runtime_launcher=$runtime_path/csaf
assert_real_path_chain "$data_root/versions" directory
assert_real_path_chain "$runtime_path" directory
assert_real_path_chain "$runtime_launcher" file
[ -x "$runtime_launcher" ] || bootstrap_required
assert_real_path_chain "$data_root/officecli" directory
assert_real_path_chain "$data_root/officecli/$officecli_version" directory
assert_real_path_chain "$officecli_path" file

CSAF_DATA_ROOT=$data_root
CSAF_OFFICECLI=$officecli_path
OFFICECLI_SKIP_UPDATE=1
PYTHONPATH=$runtime_path/site-packages
PYTHONNOUSERSITE=1
export CSAF_DATA_ROOT CSAF_OFFICECLI OFFICECLI_SKIP_UPDATE PYTHONPATH PYTHONNOUSERSITE

update_output=$("$runtime_launcher" setup check-update 2>/dev/null) || update_output=""
case "$update_output" in
    *"Update available."*)
        printf '%s\n' "CSAF update available. Run csaf setup update after explicit consent." >&2
        ;;
esac
exec "$runtime_launcher" "$@"
