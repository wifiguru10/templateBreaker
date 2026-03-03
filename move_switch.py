#!/usr/bin/python3

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import click
import meraki

import get_keys as g
from bcolors import bcolors as bc


is_exist = os.path.exists("Logs")
if not is_exist:
    os.makedirs("Logs")


db = meraki.DashboardAPI(
    api_key=g.get_api_key(),
    base_url="https://api.meraki.com/api/v1/",
    output_log=True,
    log_file_prefix=os.path.basename(__file__)[:-3],
    log_path="Logs/",
    print_console=False,
)

PORT_BACKUP_ROOT = os.path.join("Logs", "switch_port_backups")
WRITABLE_PORT_FIELDS = {
    "name",
    "tags",
    "enabled",
    "poeEnabled",
    "type",
    "vlan",
    "voiceVlan",
    "allowedVlans",
    "isolationEnabled",
    "rstpEnabled",
    "stpGuard",
    "linkNegotiation",
    "portScheduleId",
    "udld",
    "accessPolicyType",
    "accessPolicyNumber",
    "macAllowList",
    "macWhitelistLimit",
    "stickyMacAllowList",
    "stickyMacAllowListLimit",
    "stormControlEnabled",
    "adaptivePolicyGroupId",
    "peerSgtCapable",
    "flexibleStackingEnabled",
    "daiTrusted",
    "profile",
    "dot3az",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def load_org_whitelist() -> List[str]:
    whitelist = []
    path = "org_whitelist.txt"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle.readlines():
                line = line.strip()
                if line:
                    whitelist.append(line)
    return whitelist


def is_net_id(value: str) -> bool:
    return len(value) == 20 and value[1] == "_"


def find_name(items: List[Dict], target_name: str) -> List[Dict]:
    results = []
    for item in items:
        if target_name.lower() in item["name"].lower():
            results.append(item)
    return results


def get_candidate_orgs(org_whitelist: List[str]) -> List[Dict]:
    orgs_raw = db.organizations.getOrganizations()
    orgs = []
    for org in orgs_raw:
        if org_whitelist:
            if org["id"] in org_whitelist:
                orgs.append(org)
        elif org.get("api", {}).get("enabled", False):
            orgs.append(org)
    return orgs


def resolve_org(org_input: str, orgs: List[Dict]) -> Optional[str]:
    if not org_input:
        return None
    for org in orgs:
        if org["id"] == org_input:
            return org["id"]
    for org in orgs:
        if org["name"].lower() == org_input.lower():
            return org["id"]
    partials = find_name(orgs, org_input)
    if len(partials) == 1:
        return partials[0]["id"]
    return None


def resolve_network(network_input: str, orgs: List[Dict], forced_org: Optional[str]) -> Optional[Dict]:
    if is_net_id(network_input):
        try:
            network = db.networks.getNetwork(network_input)
            if forced_org and network["organizationId"] != forced_org:
                return None
            return network
        except meraki.APIError:
            return None

    search_org_ids = [forced_org] if forced_org else [org["id"] for org in orgs]
    exact_matches = []
    partial_matches = []
    for org_id in search_org_ids:
        networks = db.organizations.getOrganizationNetworks(org_id, total_pages="all")
        for network in networks:
            if network["name"].lower() == network_input.lower():
                exact_matches.append(network)
            elif network_input.lower() in network["name"].lower():
                partial_matches.append(network)
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None
    if len(partial_matches) == 1:
        return partial_matches[0]
    return None


def resolve_template(
    template_input: str, orgs: List[Dict], forced_org: Optional[str]
) -> Optional[Tuple[str, Dict]]:
    search_org_ids = [forced_org] if forced_org else [org["id"] for org in orgs]
    exact_matches: List[Tuple[str, Dict]] = []
    partial_matches: List[Tuple[str, Dict]] = []
    for org_id in search_org_ids:
        templates = db.organizations.getOrganizationConfigTemplates(org_id)
        for template in templates:
            if template["id"] == template_input or template["name"].lower() == template_input.lower():
                exact_matches.append((org_id, template))
            elif template_input.lower() in template["name"].lower():
                partial_matches.append((org_id, template))
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None
    if len(partial_matches) == 1:
        return partial_matches[0]
    return None


def get_switch_serials(network_id: str) -> List[str]:
    devices = db.networks.getNetworkDevices(network_id)
    serials = []
    for device in devices:
        product = device.get("productType")
        model = device.get("model", "")
        if product == "switch" or model.startswith("MS") or model.startswith("C9"):
            serials.append(device["serial"])
    return serials


def get_switch_models(network_id: str) -> Dict[str, str]:
    models: Dict[str, str] = {}
    devices = db.networks.getNetworkDevices(network_id)
    for device in devices:
        product = device.get("productType")
        model = device.get("model", "")
        if product == "switch" or model.startswith("MS") or model.startswith("C9"):
            models[device["serial"]] = model or "unknown"
    return models


def port_sort_key(port_id: str) -> Tuple[int, str]:
    if port_id.isdigit():
        return (0, f"{int(port_id):06d}")
    return (1, port_id)


def truncate_cell(value: Any, max_len: int = 44) -> str:
    text = str(value).replace("\n", "\\n")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def vis_len(text: str) -> int:
    return len(strip_ansi(text))


def pad_vis(text: str, width: int) -> str:
    return text + (" " * max(0, width - vis_len(text)))


def colorize_table_cell(text: str) -> str:
    raw = text.strip().lower()
    if raw in ("safe", "success", "ok", "updated"):
        return f"{bc.OKGREEN}{text}{bc.ENDC}"
    if raw in ("unstable", "failed", "mismatch-after-replay", "mismatch", "n/a", "dry-run"):
        return f"{bc.WARNING}{text}{bc.ENDC}"
    if raw in ("empty",):
        return f"{bc.DarkGray}{text}{bc.ENDC}"
    if raw in ("failed-bind-verify",):
        return f"{bc.FAIL}{text}{bc.ENDC}"
    if raw in ("override ports detected", "replay failures", "ports still different"):
        return f"{bc.WARNING}{text}{bc.ENDC}"
    if raw.endswith("%"):
        try:
            pct = float(raw.replace("%", ""))
            if pct >= 99.99:
                return f"{bc.OKGREEN}{text}{bc.ENDC}"
            if pct >= 95.0:
                return f"{bc.WARNING}{text}{bc.ENDC}"
            return f"{bc.FAIL}{text}{bc.ENDC}"
        except ValueError:
            pass
    if "failed" in raw or "error" in raw or "missing" in raw:
        return f"{bc.FAIL}{text}{bc.ENDC}"
    return text


def format_ascii_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return f"{bc.DarkGray}(no rows){bc.ENDC}"

    string_rows_plain: List[List[str]] = []
    widths = [len(h) for h in headers]
    for row in rows:
        s_row = [truncate_cell(cell) for cell in row]
        string_rows_plain.append(s_row)
        for idx, cell in enumerate(s_row):
            widths[idx] = max(widths[idx], len(cell))

    sep_plain = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    sep = f"{bc.DarkGray}{sep_plain}{bc.ENDC}"
    header_line = (
        "| "
        + " | ".join(f"{bc.OKBLUE}{pad_vis(headers[i], widths[i])}{bc.ENDC}" for i in range(len(headers)))
        + " |"
    )
    out = [sep, header_line, sep]
    for row in string_rows_plain:
        line = (
            "| "
            + " | ".join(pad_vis(colorize_table_cell(row[i]), widths[i]) for i in range(len(headers)))
            + " |"
        )
        out.append(line)
    out.append(sep)
    return "\n".join(out)


def normalize_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: normalize_obj(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        normalized = [normalize_obj(v) for v in value]
        try:
            return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True))
        except TypeError:
            return normalized
    return value


def flatten_obj(value: Any, prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(value, dict):
        for k in sorted(value.keys()):
            key = f"{prefix}.{k}" if prefix else k
            out.update(flatten_obj(value[k], key))
        return out
    if isinstance(value, list):
        out[prefix] = json.dumps(value, sort_keys=True)
        return out
    out[prefix] = str(value)
    return out


def snapshot_switch_ports(network_id: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    snapshot: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for serial in get_switch_serials(network_id):
        ports = db.switch.getDeviceSwitchPorts(serial)
        snapshot[serial] = {}
        for port in ports:
            port_id = str(port.get("portId"))
            normalized = dict(port)
            normalized.pop("portId", None)
            snapshot[serial][port_id] = normalize_obj(normalized)
    return snapshot


def snapshot_to_port_list(snapshot: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    ports: List[Dict[str, Any]] = []
    for port_id in sorted(snapshot.keys(), key=port_sort_key):
        entry = {"portId": port_id}
        entry.update(snapshot[port_id])
        ports.append(entry)
    return ports


def build_port_match_map(
    before_ports: Dict[str, Dict[str, Any]], after_ports: Dict[str, Dict[str, Any]]
) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    all_ports = sorted(set(before_ports.keys()) | set(after_ports.keys()), key=port_sort_key)
    for port_id in all_ports:
        if port_id not in before_ports or port_id not in after_ports:
            result[port_id] = False
            continue
        result[port_id] = normalize_obj(before_ports[port_id]) == normalize_obj(after_ports[port_id])
    return result


def render_switch_ascii(serial: str, model: str, port_matches: Dict[str, bool]) -> str:
    ports = sorted(port_matches.keys(), key=port_sort_key)
    total = len(ports)
    if total <= 16:
        cols = 8
    elif total <= 32:
        cols = 12
    elif total <= 52:
        cols = 12
    else:
        cols = 16

    cell_w = 7
    width = cols * cell_w + 2
    top = f"+{'=' * width}+"
    name = f" SWITCH {serial} ({model}) "
    name = name[:width].center(width)
    lines = [top, f"|{name}|", f"+{'-' * width}+"]

    row: List[str] = []
    for idx, port_id in enumerate(ports, start=1):
        ok = port_matches[port_id]
        mark = f"{bc.OKGREEN}[v]{bc.ENDC}" if ok else f"{bc.FAIL}[x]{bc.ENDC}"
        token = f"{str(port_id).rjust(2)}{mark}"
        row.append(token.ljust(cell_w))
        if len(row) == cols or idx == len(ports):
            while len(row) < cols:
                row.append(" " * cell_w)
            lines.append("| " + "".join(row) + "|")
            row = []
    lines.append(top)
    return "\n".join(lines)


def write_last_good_state_files(
    network_id: str, switch_snapshot: Dict[str, Dict[str, Dict[str, Any]]], serials: Optional[List[str]] = None
) -> List[str]:
    target_serials = sorted(serials) if serials else sorted(switch_snapshot.keys())
    written_files: List[str] = []
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for serial in target_serials:
        if serial not in switch_snapshot:
            continue
        path = f"SWITCH_{serial}.JSON"
        payload = {
            "serial": serial,
            "networkId": network_id,
            "capturedAt": ts,
            "state": "last_good",
            "ports": snapshot_to_port_list(switch_snapshot[serial]),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        written_files.append(path)
    return written_files


def backup_switch_ports(network_id: str) -> Tuple[str, Dict[str, Dict[str, Dict[str, Any]]]]:
    backup_dir = os.path.join(PORT_BACKUP_ROOT, network_id)
    os.makedirs(backup_dir, exist_ok=True)
    snapshot: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for serial in get_switch_serials(network_id):
        ports = db.switch.getDeviceSwitchPorts(serial)
        backup_path = os.path.join(backup_dir, f"{serial}.JSON")
        payload = {
            "networkId": network_id,
            "serial": serial,
            "ports": ports,
        }
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

        snapshot[serial] = {}
        for port in ports:
            port_id = str(port.get("portId"))
            normalized = dict(port)
            normalized.pop("portId", None)
            snapshot[serial][port_id] = normalize_obj(normalized)
    return backup_dir, snapshot


def replay_saved_port_overrides(network_id: str, backup_dir: str) -> Dict[str, Any]:
    replay_rows: List[List[Any]] = []
    per_switch: Dict[str, Dict[str, int]] = {}

    if not os.path.isdir(backup_dir):
        return {"replay_rows": replay_rows, "per_switch": per_switch}

    for name in sorted(os.listdir(backup_dir)):
        if not name.endswith(".JSON"):
            continue
        serial = name[:-5]
        path = os.path.join(backup_dir, name)
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)

        per_switch.setdefault(serial, {"ports_attempted": 0, "ports_updated": 0, "ports_failed": 0})

        for port in saved.get("ports", []):
            port_id = str(port.get("portId"))
            per_switch[serial]["ports_attempted"] += 1
            payload = {k: v for k, v in port.items() if k in WRITABLE_PORT_FIELDS}
            try:
                db.switch.updateDeviceSwitchPort(serial, port_id, **payload)
                per_switch[serial]["ports_updated"] += 1
                replay_rows.append([serial, port_id, "updated", "OK"])
            except meraki.APIError as err:
                per_switch[serial]["ports_failed"] += 1
                replay_rows.append([serial, port_id, "failed", str(err)])

    return {"replay_rows": replay_rows, "per_switch": per_switch}


def compare_port_snapshots(
    before: Dict[str, Dict[str, Dict[str, Any]]],
    after: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    diff_rows: List[List[str]] = []
    unchanged_ports = 0
    changed_ports = 0
    missing_ports = 0
    added_ports = 0
    total_ports_before = 0
    total_settings = 0
    changed_settings = 0
    per_switch: Dict[str, Dict[str, int]] = {}

    all_switches = sorted(set(before.keys()) | set(after.keys()))
    for serial in all_switches:
        before_ports = before.get(serial, {})
        after_ports = after.get(serial, {})
        per_switch.setdefault(
            serial,
            {
                "total_ports_before": 0,
                "changed_ports": 0,
                "unchanged_ports": 0,
                "missing_ports": 0,
                "added_ports": 0,
                "changed_settings": 0,
                "total_settings": 0,
            },
        )
        all_ports = sorted(set(before_ports.keys()) | set(after_ports.keys()), key=port_sort_key)

        for port_id in all_ports:
            b_port = before_ports.get(port_id)
            a_port = after_ports.get(port_id)

            if b_port is None:
                added_ports += 1
                per_switch[serial]["added_ports"] += 1
                diff_rows.append([serial, port_id, "__port__", "(missing)", "added in destination"])
                continue
            total_ports_before += 1
            per_switch[serial]["total_ports_before"] += 1
            if a_port is None:
                missing_ports += 1
                changed_ports += 1
                per_switch[serial]["missing_ports"] += 1
                per_switch[serial]["changed_ports"] += 1
                diff_rows.append([serial, port_id, "__port__", "exists in source", "(missing)"])
                continue

            flat_before = flatten_obj(b_port)
            flat_after = flatten_obj(a_port)
            all_keys = sorted(set(flat_before.keys()) | set(flat_after.keys()))
            port_changed = False
            for key in all_keys:
                before_val = flat_before.get(key, "(missing)")
                after_val = flat_after.get(key, "(missing)")
                total_settings += 1
                per_switch[serial]["total_settings"] += 1
                if before_val != after_val:
                    changed_settings += 1
                    per_switch[serial]["changed_settings"] += 1
                    port_changed = True
                    diff_rows.append([serial, port_id, key, before_val, after_val])
            if port_changed:
                changed_ports += 1
                per_switch[serial]["changed_ports"] += 1
            else:
                unchanged_ports += 1
                per_switch[serial]["unchanged_ports"] += 1

    port_success_rate = 100.0 if total_ports_before == 0 else (unchanged_ports / total_ports_before) * 100.0
    settings_success_rate = (
        100.0 if total_settings == 0 else ((total_settings - changed_settings) / total_settings) * 100.0
    )

    return {
        "diff_rows": diff_rows,
        "unchanged_ports": unchanged_ports,
        "changed_ports": changed_ports,
        "missing_ports": missing_ports,
        "added_ports": added_ports,
        "total_ports_before": total_ports_before,
        "total_settings": total_settings,
        "changed_settings": changed_settings,
        "port_success_rate": port_success_rate,
        "settings_success_rate": settings_success_rate,
        "per_switch": per_switch,
    }


def get_bound_switch_networks_in_template(org_id: str, template_id: str) -> List[Dict]:
    networks = db.organizations.getOrganizationNetworks(org_id, total_pages="all")
    result = []
    for network in networks:
        if "switch" not in network.get("productTypes", []):
            continue
        if network.get("configTemplateId") == template_id:
            result.append(network)
    result.sort(key=lambda n: n["name"].lower())
    return result


def move_and_validate_network(network: Dict, template: Dict, autobind: bool, dry_run: bool) -> Dict[str, Any]:
    network_id = network["id"]
    source_template_id = network.get("configTemplateId")
    switch_serials_before = get_switch_serials(network_id)
    switch_models = get_switch_models(network_id)
    backup_dir = os.path.join(PORT_BACKUP_ROOT, network_id)
    before_snapshot = snapshot_switch_ports(network_id)

    print()
    print(
        f"{bc.HEADER}{bc.BOLD}Network Move{bc.ENDC} "
        f"{bc.OKGREEN}[{bc.WARNING}{network['name']}{bc.OKGREEN}] "
        f"netID[{bc.WARNING}{network_id}{bc.OKGREEN}] "
        f"switches[{bc.WARNING}{len(switch_serials_before)}{bc.OKGREEN}]{bc.ENDC}"
    )

    if dry_run:
        print(f"{bc.WARNING}DRY RUN - no changes will be made{bc.ENDC}")
        print(
            f"{bc.LightBlue}Would save switch port backups to "
            f"[{os.path.join(backup_dir, '<SERIAL>.JSON')}]{bc.ENDC}"
        )
        print(f"{bc.LightBlue}Would update local last-good files [SWITCH_<SERIAL>.JSON]{bc.ENDC}")
        if source_template_id:
            print(
                f"{bc.LightBlue}Would unbind network [{network_id}] from template "
                f"[{source_template_id}] with retainConfigs=True{bc.ENDC}"
            )
        else:
            print(f"{bc.LightBlue}Would skip unbind because network [{network_id}] is not template-bound{bc.ENDC}")
        print(
            f"{bc.LightBlue}Would bind network [{network_id}] to template [{template['id']}] "
            f"with autoBind={autobind}{bc.ENDC}"
        )
        print(
            f"{bc.Cyan}Switch serials currently in network ({len(switch_serials_before)}): "
            f"{', '.join(switch_serials_before) if switch_serials_before else '(none)'}{bc.ENDC}"
        )
        return {
            "network_id": network_id,
            "network_name": network["name"],
            "status": "dry-run",
            "diff": None,
        }

    if source_template_id:
        print(
            f"{bc.OKGREEN}Unbinding current template [{bc.WARNING}{source_template_id}{bc.OKGREEN}] "
            f"with retainConfigs=True...{bc.ENDC}"
        )
        backup_dir, before_snapshot = backup_switch_ports(network_id)
        print(
            f"{bc.OKGREEN}Saved switch port backups to [{bc.WARNING}{backup_dir}{bc.OKGREEN}] "
            f"as <SERIAL>.JSON files{bc.ENDC}"
        )
        written = write_last_good_state_files(network_id, before_snapshot)
        print(
            f"{bc.OKGREEN}Updated local last-good files:{bc.ENDC} "
            f"{bc.WARNING}{', '.join(written) if written else '(none)'}{bc.ENDC}"
        )
        db.networks.unbindNetwork(network_id, retainConfigs=True)
    else:
        print(f"{bc.WARNING}Network is not currently template-bound; skipping unbind.{bc.ENDC}")

    print(
        f"{bc.OKGREEN}Binding to target template [{bc.WARNING}{template['id']}{bc.OKGREEN}] "
        f"with autoBind={autobind}...{bc.ENDC}"
    )
    db.networks.bindNetwork(network_id, configTemplateId=template["id"], autoBind=autobind)

    refreshed = db.networks.getNetwork(network_id)
    if refreshed.get("configTemplateId") != template["id"]:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} bind operation completed but network does not show expected template ID."
            f"{bc.ENDC}"
        )
        return {
            "network_id": network_id,
            "network_name": network["name"],
            "status": "failed-bind-verify",
            "diff": None,
        }

    switch_serials_after = get_switch_serials(network_id)
    missing_switches = sorted(set(switch_serials_before) - set(switch_serials_after))

    after_bind_snapshot = snapshot_switch_ports(network_id)
    bind_diff = compare_port_snapshots(before_snapshot, after_bind_snapshot)

    print(f"{bc.WARNING}Port Configuration Diff Report (Before vs After Rebind){bc.ENDC}")
    if bind_diff["diff_rows"]:
        headers = ["Switch", "Port", "Setting", "Before", "After"]
        print(format_ascii_table(headers, bind_diff["diff_rows"]))
    else:
        print(f"{bc.OKGREEN}No port configuration differences detected after rebind.{bc.ENDC}")

    print()
    print(f"{bc.OKBLUE}Applying saved pre-move port overrides back to switches...{bc.ENDC}")
    replay_result = replay_saved_port_overrides(network_id, backup_dir)
    after_replay_snapshot = snapshot_switch_ports(network_id)
    final_diff = compare_port_snapshots(before_snapshot, after_replay_snapshot)

    print()
    print(f"{bc.WARNING}Replay Actions (<SERIAL>.JSON -> Switch Port Updates){bc.ENDC}")
    if replay_result["replay_rows"]:
        print(format_ascii_table(["Switch", "Port", "Action", "Result"], replay_result["replay_rows"]))
    else:
        print(f"{bc.WARNING}No replay actions were executed.{bc.ENDC}")

    print()
    print(f"{bc.WARNING}Remaining Differences After Override Replay{bc.ENDC}")
    if final_diff["diff_rows"]:
        headers = ["Switch", "Port", "Setting", "Before", "After"]
        print(format_ascii_table(headers, final_diff["diff_rows"]))
        print(
            f"{bc.FAIL}VALIDATION RESULT:{bc.ENDC} {bc.WARNING}MISMATCH{bc.ENDC} - "
            f"current state still differs from pre-move configuration."
        )
    else:
        print(f"{bc.OKGREEN}No remaining differences. Overrides restored successfully.{bc.ENDC}")
        print(
            f"{bc.OKGREEN}VALIDATION RESULT:{bc.ENDC} {bc.OKBLUE}SUCCESS{bc.ENDC} - "
            f"current state matches pre-move configuration."
        )

    print()
    print(
        f"{bc.OKBLUE}Port success rate:{bc.ENDC} {bc.OKGREEN}{final_diff['port_success_rate']:.2f}%{bc.ENDC} "
        f"({final_diff['unchanged_ports']}/{final_diff['total_ports_before']} unchanged ports)"
    )
    print(
        f"{bc.OKBLUE}Settings success rate:{bc.ENDC} "
        f"{bc.OKGREEN}{final_diff['settings_success_rate']:.2f}%{bc.ENDC} "
        f"({final_diff['total_settings'] - final_diff['changed_settings']}/{final_diff['total_settings']} "
        f"unchanged settings)"
    )
    if missing_switches:
        print(
            f"{bc.FAIL}WARNING:{bc.OKGREEN} switches missing after move: "
            f"{bc.WARNING}{missing_switches}{bc.ENDC}"
        )
    else:
        print(f"{bc.OKGREEN}Switch inventory matched before/after move.{bc.ENDC}")

    print()
    print(f"{bc.HEADER}{bc.BOLD}Per-Switch Visual Diff Panels{bc.ENDC}")
    replay_rows_by_switch: Dict[str, Dict[str, str]] = {}
    for row in replay_result["replay_rows"]:
        serial, port_id, action, result_text = row
        replay_rows_by_switch.setdefault(serial, {})[str(port_id)] = f"{action}:{result_text}"

    all_switches_for_panels = sorted(set(before_snapshot.keys()) | set(after_bind_snapshot.keys()))
    for serial in all_switches_for_panels:
        model = switch_models.get(serial, "unknown")
        before_ports = before_snapshot.get(serial, {})
        # Visual panel is intentionally based on post-rebind state so red ports show required overrides.
        after_rebind_ports = after_bind_snapshot.get(serial, {})
        port_matches = build_port_match_map(before_ports, after_rebind_ports)

        print()
        print(render_switch_ascii(serial, model, port_matches))

        override_rows = []
        for d_serial, d_port, d_setting, d_before, d_after in bind_diff["diff_rows"]:
            if d_serial != serial or d_setting == "__port__":
                continue
            replay_state = replay_rows_by_switch.get(serial, {}).get(str(d_port), "n/a")
            override_rows.append([d_port, d_setting, d_before, d_after, replay_state])

        remaining_rows = []
        for d_serial, d_port, d_setting, d_before, d_after in final_diff["diff_rows"]:
            if d_serial != serial or d_setting == "__port__":
                continue
            remaining_rows.append([d_port, d_setting, d_before, d_after])

        diff_count = sum(1 for matched in port_matches.values() if not matched)
        print(
            f"{bc.OKBLUE}Ports matching original (after rebind):{bc.ENDC} "
            f"{bc.OKGREEN}{len(port_matches) - diff_count}{bc.ENDC}/{len(port_matches)}  "
            f"{bc.OKBLUE}Override ports detected:{bc.ENDC} {bc.WARNING}{diff_count}{bc.ENDC}"
        )

        print(f"{bc.WARNING}Overrides Applied From BEFORE (After Rebind -> Before){bc.ENDC}")
        if override_rows:
            print(format_ascii_table(["Port", "Setting", "Before", "After Rebind", "Replay Action"], override_rows))
        else:
            print(f"{bc.DarkGray}(no overrides required for this switch){bc.ENDC}")

        print(f"{bc.WARNING}Remaining Differences vs BEFORE (After Replay){bc.ENDC}")
        if remaining_rows:
            print(format_ascii_table(["Port", "Setting", "Before", "Current"], remaining_rows))
        else:
            print(f"{bc.OKGREEN}All port-level state matches BEFORE for this switch.{bc.ENDC}")

    switch_summary_rows: List[List[Any]] = []
    all_switches = sorted(set(bind_diff["per_switch"].keys()) | set(final_diff["per_switch"].keys()))
    for serial in all_switches:
        bind_stats = bind_diff["per_switch"].get(serial, {})
        final_stats = final_diff["per_switch"].get(serial, {})
        replay_stats = replay_result["per_switch"].get(serial, {"ports_updated": 0, "ports_failed": 0})
        switch_summary_rows.append(
            [
                serial,
                bind_stats.get("changed_ports", 0),
                bind_stats.get("total_ports_before", 0),
                final_stats.get("changed_ports", 0),
                replay_stats.get("ports_updated", 0),
                replay_stats.get("ports_failed", 0),
            ]
        )

    print()
    print(f"{bc.HEADER}{bc.BOLD}Per-Switch Override Summary{bc.ENDC}")
    print(
        format_ascii_table(
            [
                "Switch",
                "Ports Different After Rebind",
                "Total Ports",
                "Ports Still Different",
                "Ports Replayed",
                "Replay Failures",
            ],
            switch_summary_rows,
        )
    )

    return {
        "network_id": network_id,
        "network_name": network["name"],
        "status": "success" if not final_diff["diff_rows"] else "mismatch-after-replay",
        "diff": final_diff,
    }


@click.command()
@click.argument("source", default="")
@click.argument("target_template", default="")
@click.argument("organization", default="")
@click.option(
    "--autobind/--no-autobind",
    default=True,
    show_default=True,
    help="When binding to target template, auto-bind switch profiles by model.",
)
@click.option(
    "--dry-run/--execute",
    default=True,
    show_default=True,
    help="Dry-run is default. Use --execute to apply changes.",
)
@click.option(
    "--all-in-template",
    is_flag=True,
    default=False,
    help="Move all switch networks bound to the source network's current template.",
)
def move_switch(
    source: str, target_template: str, organization: str, autobind: bool, dry_run: bool, all_in_template: bool
):
    """
    Move a switch network from its current template to a different template.

    SOURCE can be a Network ID or exact/partial name.
    TARGET_TEMPLATE can be a Template ID or exact/partial name.
    ORGANIZATION can be an Org ID or exact/partial name (optional).
    """
    if not source:
        source = input(f"{bc.OKBLUE}Source Network (ID or name)> {bc.ENDC}").strip()
    if not target_template:
        target_template = input(f"{bc.OKBLUE}Destination Template (ID or name)> {bc.ENDC}").strip()

    org_whitelist = load_org_whitelist()
    orgs = get_candidate_orgs(org_whitelist)
    if not orgs:
        print(f"{bc.FAIL}ERROR:{bc.OKGREEN} no organizations available from API key/whitelist.{bc.ENDC}")
        return

    forced_org = resolve_org(organization.strip(), orgs) if organization else None
    if organization and not forced_org:
        print(f"{bc.FAIL}ERROR:{bc.OKGREEN} organization '{organization}' was not uniquely found.{bc.ENDC}")
        return

    network = resolve_network(source.strip(), orgs, forced_org)
    if not network:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} source network '{source}' was not uniquely found. "
            f"Use a network ID to disambiguate.{bc.ENDC}"
        )
        return

    network_org_id = network["organizationId"]
    product_types = network.get("productTypes", [])
    if "switch" not in product_types:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} network '{network['name']}' is not a switch network "
            f"(productTypes={product_types}).{bc.ENDC}"
        )
        return

    template_org_id = forced_org or network_org_id
    template_match = resolve_template(target_template.strip(), orgs, template_org_id)
    if not template_match:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} target template '{target_template}' was not uniquely found "
            f"in org '{template_org_id}'.{bc.ENDC}"
        )
        return

    target_org_id, template = template_match
    if target_org_id != network_org_id:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} template and network are in different orgs. "
            f"Network org={network_org_id}, template org={target_org_id}.{bc.ENDC}"
        )
        return

    if "switch" not in template.get("productTypes", []):
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} template '{template['name']}' is not a switch template "
            f"(productTypes={template.get('productTypes', [])}).{bc.ENDC}"
        )
        return

    source_template_id = network.get("configTemplateId")
    if not source_template_id:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} source network is not bound to a template. "
            f"This workflow requires a bound source template.{bc.ENDC}"
        )
        return
    if source_template_id == template["id"]:
        print(f"{bc.WARNING}No changes needed:{bc.OKGREEN} source template already equals target template.{bc.ENDC}")
        return

    if all_in_template:
        networks_to_move = get_bound_switch_networks_in_template(network_org_id, source_template_id)
        if not networks_to_move:
            print(
                f"{bc.FAIL}ERROR:{bc.OKGREEN} no bound switch networks found on source template "
                f"[{bc.WARNING}{source_template_id}{bc.OKGREEN}].{bc.ENDC}"
            )
            return
        print(
            f"{bc.OKGREEN}Preparing to move [{bc.WARNING}{len(networks_to_move)}{bc.OKGREEN}] switch network(s) "
            f"from template [{bc.WARNING}{source_template_id}{bc.OKGREEN}] to "
            f"[{bc.WARNING}{template['id']}{bc.OKGREEN}] autoBind[{bc.WARNING}{autobind}{bc.OKGREEN}] "
            f"dryRun[{bc.WARNING}{dry_run}{bc.OKGREEN}] mode[{bc.WARNING}all-in-template{bc.OKGREEN}]{bc.ENDC}"
        )
    else:
        networks_to_move = [network]
        print(
            f"{bc.OKGREEN}Preparing to move single network "
            f"[{bc.WARNING}{network['name']}{bc.OKGREEN}] "
            f"to template [{bc.WARNING}{template['id']}{bc.OKGREEN}] "
            f"autoBind[{bc.WARNING}{autobind}{bc.OKGREEN}] "
            f"dryRun[{bc.WARNING}{dry_run}{bc.OKGREEN}] mode[{bc.WARNING}single{bc.OKGREEN}]{bc.ENDC}"
        )

    results = []
    for net in networks_to_move:
        result = move_and_validate_network(net, template, autobind, dry_run)
        results.append(result)

    print()
    print(f"{bc.HEADER}{bc.BOLD}Per-Network Summary{bc.ENDC}")
    summary_rows: List[List[Any]] = []
    total_port_rates = 0.0
    total_settings_rates = 0.0
    measured_networks = 0
    for result in results:
        if result["status"] == "success" and result["diff"] is not None:
            diff = result["diff"]
            port_rate = diff["port_success_rate"]
            settings_rate = diff["settings_success_rate"]
            measured_networks += 1
            total_port_rates += port_rate
            total_settings_rates += settings_rate
        else:
            port_rate = "n/a"
            settings_rate = "n/a"
        summary_rows.append(
            [
                result["network_name"],
                result["network_id"],
                result["status"],
                port_rate if isinstance(port_rate, str) else f"{port_rate:.2f}%",
                settings_rate if isinstance(settings_rate, str) else f"{settings_rate:.2f}%",
            ]
        )

    print(format_ascii_table(["Network", "Network ID", "Status", "Port Success", "Settings Success"], summary_rows))

    if measured_networks > 0:
        avg_port = total_port_rates / measured_networks
        avg_settings = total_settings_rates / measured_networks
        print()
        print(
            f"{bc.OKBLUE}Average port success rate:{bc.ENDC} "
            f"{bc.OKGREEN}{avg_port:.2f}%{bc.ENDC} across {measured_networks} network(s)"
        )
        print(
            f"{bc.OKBLUE}Average settings success rate:{bc.ENDC} "
            f"{bc.OKGREEN}{avg_settings:.2f}%{bc.ENDC} across {measured_networks} network(s)"
        )


if __name__ == "__main__":
    move_switch()
