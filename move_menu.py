#!/usr/bin/python3

import os
import re
import shutil
import subprocess
import sys
import textwrap
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


def get_candidate_orgs(org_whitelist: List[str]) -> List[Dict]:
    orgs_raw = db.organizations.getOrganizations()
    orgs = []
    for org in orgs_raw:
        if org_whitelist:
            if org["id"] in org_whitelist:
                orgs.append(org)
        elif org.get("api", {}).get("enabled", False):
            orgs.append(org)
    orgs.sort(key=lambda o: o["name"].lower())
    return orgs


def get_templates(org_id: str) -> List[Dict]:
    templates = db.organizations.getOrganizationConfigTemplates(org_id)
    switch_templates = [t for t in templates if "switch" in t.get("productTypes", [])]
    switch_templates.sort(key=lambda t: t["name"].lower())
    return switch_templates


def get_bound_switch_networks(org_id: str) -> List[Dict]:
    networks = db.organizations.getOrganizationNetworks(org_id, total_pages="all")
    bound_switch_networks = []
    for network in networks:
        if "switch" not in network.get("productTypes", []):
            continue
        if "configTemplateId" not in network:
            continue
        bound_switch_networks.append(network)
    bound_switch_networks.sort(key=lambda n: n["name"].lower())
    return bound_switch_networks


def is_switch_device(device: Dict[str, Any]) -> bool:
    return device.get("productType") == "switch" or str(device.get("model", "")).startswith(("MS", "C9"))


def normalize_status_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def connection_is_good(status_text: str) -> bool:
    text = normalize_status_text(status_text)
    if not text:
        return False
    if text in ("online", "alerting"):
        return True
    bad_tokens = ("offline", "dormant", "disconnected", "not connected")
    if any(token in text for token in bad_tokens):
        return False
    return False


def get_org_switch_health(
    org_id: str, networks: List[Dict], templates: List[Dict]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    network_health: Dict[str, Dict[str, Any]] = {}
    template_health: Dict[str, Dict[str, Any]] = {}

    network_ids = [n["id"] for n in networks]
    if not network_ids:
        return network_health, template_health

    try:
        org_devices = db.organizations.getOrganizationDevices(org_id, total_pages="all", networkIds=network_ids)
    except meraki.APIError:
        org_devices = []
    try:
        dev_statuses = db.organizations.getOrganizationDevicesStatuses(
            org_id, total_pages="all", networkIds=network_ids, productTypes=["switch"]
        )
    except meraki.APIError:
        dev_statuses = []

    status_by_serial = {item.get("serial"): item for item in dev_statuses if item.get("serial")}

    switches_by_network: Dict[str, List[Dict[str, Any]]] = {}
    for device in org_devices:
        if not is_switch_device(device):
            continue
        nid = device.get("networkId")
        if not nid:
            continue
        switches_by_network.setdefault(nid, []).append(device)

    for network in networks:
        nid = network["id"]
        switches = switches_by_network.get(nid, [])
        bad_connectivity = 0
        status_unknown = 0
        for device in switches:
            serial = device.get("serial")
            status_obj = status_by_serial.get(serial, {})
            raw_status = normalize_status_text(status_obj.get("status"))
            if not raw_status:
                status_unknown += 1
                bad_connectivity += 1
            elif not connection_is_good(raw_status):
                bad_connectivity += 1

        safe = len(switches) > 0 and bad_connectivity == 0
        reasons = []
        if len(switches) == 0:
            reasons.append("no switch devices")
        if bad_connectivity:
            reasons.append(f"offline/not-connected {bad_connectivity}")
        if status_unknown:
            reasons.append(f"unknown-status {status_unknown}")
        reason = "ok" if not reasons else ", ".join(reasons)
        status = "SAFE" if safe else "UNSTABLE"

        network_health[nid] = {
            "device_count": len(switches),
            "status": status,
            "safe": safe,
            "reason": reason,
            "bound_template_id": network.get("configTemplateId"),
        }

    for template in templates:
        tid = template["id"]
        bound_networks = [n for n in networks if n.get("configTemplateId") == tid]
        total_devices = sum(network_health.get(n["id"], {}).get("device_count", 0) for n in bound_networks)
        if not bound_networks:
            template_health[tid] = {
                "device_count": 0,
                "network_count": 0,
                "status": "EMPTY",
                "safe": False,
                "reason": "no bound networks",
            }
            continue
        unstable_count = sum(1 for n in bound_networks if not network_health.get(n["id"], {}).get("safe", False))
        status = "SAFE" if unstable_count == 0 else "UNSTABLE"
        reason = "ok" if unstable_count == 0 else f"{unstable_count}/{len(bound_networks)} unstable network(s)"
        template_health[tid] = {
            "device_count": total_devices,
            "network_count": len(bound_networks),
            "status": status,
            "safe": unstable_count == 0,
            "reason": reason,
        }

    return network_health, template_health


def group_networks_by_template(networks: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for network in networks:
        template_id = network.get("configTemplateId")
        if not template_id:
            continue
        grouped.setdefault(template_id, []).append(network)
    return grouped


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def term_width() -> int:
    width = shutil.get_terminal_size(fallback=(120, 30)).columns
    return max(72, min(width, 220))


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def truncate_plain(text: str, max_len: int) -> str:
    if max_len < 4:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def colorize_status_tokens(text: str) -> str:
    text = text.replace("Status:SAFE", f"Status:{status_colored('SAFE')}")
    text = text.replace("Status:UNSTABLE", f"Status:{status_colored('UNSTABLE')}")
    text = text.replace("Status:EMPTY", f"Status:{status_colored('EMPTY')}")
    return text


def bar(char: str = "=", width: Optional[int] = None, color: str = bc.DarkGray) -> str:
    if width is None:
        width = term_width()
    return f"{color}{char * width}{bc.ENDC}"


def banner() -> None:
    width = term_width()
    inner = width - 2
    title = " TEMPLATE BREAKER :: SWITCH TEMPLATE MOVE CONSOLE "
    subtitle = "Guided flow: org -> source network/template -> destination template -> execution mode"
    title_line = title.center(inner)

    print()
    print(f"{bc.LightBlue}╔{'═' * inner}╗{bc.ENDC}")
    print(f"{bc.LightBlue}║{bc.BackgroundBlue}{bc.White}{bc.BOLD}{title_line}{bc.ENDC}{bc.LightBlue}║{bc.ENDC}")
    print(f"{bc.LightBlue}╚{'═' * inner}╝{bc.ENDC}")
    wrapped = textwrap.wrap(subtitle, width=max(20, width - 4))
    for line in wrapped:
        print(f"{bc.Cyan}{line}{bc.ENDC}")
    print()


def section(title: str, subtitle: str = "") -> None:
    width = term_width()
    print()
    print(bar("-", width, bc.DarkGray))
    print(f"{bc.HEADER}{bc.BOLD}{title}{bc.ENDC}")
    if subtitle:
        for line in textwrap.wrap(subtitle, width=max(20, width - 2)):
            print(f"{bc.LightCyan}{line}{bc.ENDC}")
    print(bar("-", width, bc.DarkGray))


def color_id(value: str) -> str:
    return f"{bc.WARNING}{value}{bc.ENDC}"


def status_chip(text: str, ok: bool = True) -> str:
    if ok:
        return f"{bc.BackgroundGreen}{bc.Black} {text} {bc.ENDC}"
    return f"{bc.BackgroundYellow}{bc.Black} {text} {bc.ENDC}"


def render_context(selected_org: Dict, selected_network: Dict, selected_template: Dict, selected_all: bool) -> None:
    section("Execution Plan", "Review selections before command execution")
    mode_text = "ALL NETWORKS IN SOURCE TEMPLATE" if selected_all else "SINGLE NETWORK"
    print(f"{status_chip('MODE', True)} {bc.OKBLUE}{mode_text}{bc.ENDC}")
    print(f"{bc.OKBLUE}Organization:{bc.ENDC} {bc.OKGREEN}{selected_org['name']}{bc.ENDC} [{color_id(selected_org['id'])}]")
    print(
        f"{bc.OKBLUE}Source Network:{bc.ENDC} {bc.OKGREEN}{selected_network['name']}{bc.ENDC} "
        f"[{color_id(selected_network['id'])}]"
    )
    print(
        f"{bc.OKBLUE}Destination Template:{bc.ENDC} {bc.OKGREEN}{selected_template['name']}{bc.ENDC} "
        f"[{color_id(selected_template['id'])}]"
    )


def status_colored(status: str) -> str:
    normalized = status.strip().upper()
    if normalized == "SAFE":
        return f"{bc.BackgroundGreen}{bc.Black} SAFE {bc.ENDC}"
    if normalized == "EMPTY":
        return f"{bc.BackgroundDarkGray}{bc.White} EMPTY {bc.ENDC}"
    return f"{bc.BackgroundYellow}{bc.Black} UNSTABLE {bc.ENDC}"


def ratio_bar(good: int, bad: int, width: int = 30) -> str:
    total = max(1, good + bad)
    good_w = int(round((good / total) * width))
    bad_w = max(0, width - good_w)
    return (
        f"{bc.BackgroundGreen}{' ' * good_w}{bc.ENDC}"
        f"{bc.BackgroundRed}{' ' * bad_w}{bc.ENDC}"
    )


def render_health_dashboard(network_health: Dict[str, Dict[str, Any]], template_health: Dict[str, Dict[str, Any]]) -> None:
    width = term_width()
    safe_nets = sum(1 for h in network_health.values() if h.get("status") == "SAFE")
    unstable_nets = sum(1 for h in network_health.values() if h.get("status") == "UNSTABLE")
    safe_tpl = sum(1 for h in template_health.values() if h.get("status") == "SAFE")
    unstable_tpl = sum(1 for h in template_health.values() if h.get("status") == "UNSTABLE")
    empty_tpl = sum(1 for h in template_health.values() if h.get("status") == "EMPTY")
    bar_w = max(12, min(40, width - 54))

    print(f"{bc.BOLD}{bc.HEADER}Health Dashboard{bc.ENDC}")
    print(
        f"Networks  {status_colored('SAFE')} {bc.OKGREEN}{safe_nets}{bc.ENDC}  "
        f"{status_colored('UNSTABLE')} {bc.WARNING}{unstable_nets}{bc.ENDC}  "
        f"{ratio_bar(safe_nets, unstable_nets, bar_w)}"
    )
    print(
        f"Templates {status_colored('SAFE')} {bc.OKGREEN}{safe_tpl}{bc.ENDC}  "
        f"{status_colored('UNSTABLE')} {bc.WARNING}{unstable_tpl}{bc.ENDC}  "
        f"{status_colored('EMPTY')} {bc.DarkGray}{empty_tpl}{bc.ENDC}"
    )
    print(bar("-", width, bc.DarkGray))


def select_from_list(title: str, items: List[Tuple[str, str]]) -> Optional[int]:
    width = term_width()
    usable = max(30, width - 2)
    idx_w = max(2, len(str(len(items))))
    id_w = 22
    main_w = max(16, usable - (idx_w + id_w + 7))
    detail_w = max(16, usable - (idx_w + 3))

    print()
    print(f"{bc.HEADER}{bc.BOLD}{title}{bc.ENDC}")
    print(bar("-", width, bc.DarkGray))
    for idx, (label, value) in enumerate(items, start=1):
        parts = label.split(" | ")
        head = parts[0].strip()
        tail = " | ".join(parts[1:]).strip() if len(parts) > 1 else ""
        head = truncate_plain(head, main_w)
        value_show = truncate_plain(value, id_w)
        prefix = f"{str(idx).rjust(idx_w)}."
        line = (
            f"{bc.OKBLUE}{prefix}{bc.ENDC} "
            f"{bc.OKGREEN}{head}{bc.ENDC} "
            f"[{bc.WARNING}{value_show}{bc.ENDC}]"
        )
        print(line)
        if tail:
            wrapped_tail = textwrap.wrap(tail, width=detail_w)
            for extra in wrapped_tail[:4]:
                extra = colorize_status_tokens(extra)
                print(f"{' ' * (idx_w + 2)} {bc.DarkGray}{extra}{bc.ENDC}")

        if idx != len(items):
            print(f"{bc.DarkGray}{' ' * idx_w}  {'·' * max(8, min(24, detail_w // 2))}{bc.ENDC}")

    print(bar("-", width, bc.DarkGray))
    cancel_label = "Cancel"
    print(
        f"{bc.FAIL}{'0'.rjust(idx_w)}.{bc.ENDC} "
        f"{bc.WARNING}{cancel_label}{bc.ENDC}"
    )
    print(
        f"{bc.DarkGray}Tip:{bc.ENDC} enter the index number and press Enter."
    )
    print(bar("-", width, bc.DarkGray))

    while True:
        choice = input(f"{bc.OKBLUE}>{bc.ENDC} ").strip()
        if not choice.isdigit():
            print(f"{bc.FAIL}Please enter a number.{bc.ENDC}")
            continue
        index = int(choice)
        if index == 0:
            return None
        if 1 <= index <= len(items):
            return index - 1
        print(
            f"{bc.FAIL}Enter a value between 0 and {len(items)}.{bc.ENDC}"
        )


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = f"{bc.OKBLUE}[Y/n]{bc.ENDC}" if default_yes else f"{bc.OKBLUE}[y/N]{bc.ENDC}"
    while True:
        answer = input(f"{bc.Cyan}{prompt}{bc.ENDC} {suffix} ").strip().lower()
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print(f"{bc.FAIL}Please answer y or n.{bc.ENDC}")


@click.command()
def move_menu():
    banner()
    org_whitelist = load_org_whitelist()
    orgs = get_candidate_orgs(org_whitelist)
    if not orgs:
        print(f"{bc.FAIL}ERROR:{bc.OKGREEN} no organizations available from API key/whitelist.{bc.ENDC}")
        return

    section("Step 1: Organization", f"Found {len(orgs)} accessible organization(s)")
    org_items = [(org["name"], org["id"]) for org in orgs]
    org_index = select_from_list("Select an Organization:", org_items)
    if org_index is None:
        print(f"{bc.WARNING}Cancelled.{bc.ENDC}")
        return
    selected_org = orgs[org_index]
    org_id = selected_org["id"]

    networks = get_bound_switch_networks(org_id)
    if not networks:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} no bound switch networks found in org "
            f"[{bc.WARNING}{selected_org['name']}{bc.OKGREEN}].{bc.ENDC}"
        )
        return

    templates = get_templates(org_id)
    if not templates:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} no switch templates found in org "
            f"[{bc.WARNING}{selected_org['name']}{bc.OKGREEN}].{bc.ENDC}"
        )
        return

    section("Health Check", "Evaluating switch firmware/config-sync stability for menu status columns")
    network_health, template_health = get_org_switch_health(org_id, networks, templates)
    render_health_dashboard(network_health, template_health)

    section(
        "Step 2: Source Scope",
        (
            f"Org [{selected_org['name']}] has {len(networks)} bound switch network(s) and "
            f"{len(templates)} switch template(s). "
            f"SAFE means switches are connected (online/alerting)."
        ),
    )
    template_by_id = {t["id"]: t for t in templates}
    network_items = []
    for network in networks:
        current_template = template_by_id.get(network["configTemplateId"])
        current_template_name = current_template["name"] if current_template else "Unknown"
        health = network_health.get(network["id"], {})
        status = health.get("status", "UNSTABLE")
        reason = health.get("reason", "unknown")
        device_count = health.get("device_count", 0)
        label = (
            f"{network['name']} | Devices:{device_count} | "
            f"Status:{status} | Tpl:{current_template_name} | {reason}"
        )
        network_items.append((label, network["id"]))

    network_items = [("[ALL NETWORKS] Select by source template", "ALL")] + network_items
    network_index = select_from_list("Select a bound switch network to move:", network_items)
    if network_index is None:
        print(f"{bc.WARNING}Cancelled.{bc.ENDC}")
        return

    selected_network = None
    selected_all_networks = False
    if network_items[network_index][1] == "ALL":
        selected_all_networks = True
        grouped = group_networks_by_template(networks)
        source_templates = [t for t in templates if t["id"] in grouped]
        if not source_templates:
            print(
                f"{bc.FAIL}ERROR:{bc.OKGREEN} no source templates with bound switch networks were found.{bc.ENDC}"
            )
            return
        source_template_items = []
        for source_template in source_templates:
            bound_count = len(grouped.get(source_template["id"], []))
            t_health = template_health.get(source_template["id"], {})
            status = t_health.get("status", "UNSTABLE")
            reason = t_health.get("reason", "unknown")
            device_count = t_health.get("device_count", 0)
            source_template_items.append(
                (
                    f"{source_template['name']} | Networks:{bound_count} | Devices:{device_count} | "
                    f"Status:{status} | {reason}",
                    source_template["id"],
                )
            )
        source_template_index = select_from_list(
            "Select source template for [ALL NETWORKS] move:", source_template_items
        )
        if source_template_index is None:
            print(f"{bc.WARNING}Cancelled.{bc.ENDC}")
            return
        current_template_id = source_templates[source_template_index]["id"]
        selected_network = grouped[current_template_id][0]
    else:
        selected_network = networks[network_index - 1]
        current_template_id = selected_network["configTemplateId"]

    destination_templates = [t for t in templates if t["id"] != current_template_id]
    if not destination_templates:
        print(
            f"{bc.FAIL}ERROR:{bc.OKGREEN} no alternate switch templates available for destination.{bc.ENDC}"
        )
        return

    section("Step 3: Destination Template")
    template_items = []
    for template in destination_templates:
        t_health = template_health.get(template["id"], {})
        status = t_health.get("status", "UNSTABLE")
        reason = t_health.get("reason", "unknown")
        device_count = t_health.get("device_count", 0)
        network_count = t_health.get("network_count", 0)
        label = (
            f"{template['name']} | Networks:{network_count} | Devices:{device_count} | "
            f"Status:{status} | {reason}"
        )
        template_items.append((label, template["id"]))
    template_index = select_from_list("Select destination switch template:", template_items)
    if template_index is None:
        print(f"{bc.WARNING}Cancelled.{bc.ENDC}")
        return
    selected_template = destination_templates[template_index]

    section("Step 4: Runtime Options")
    autobind = ask_yes_no("Enable autoBind for switch profile rebinding?", default_yes=True)
    dry_run = ask_yes_no("Run as dry-run only (no changes)?", default_yes=True)

    cmd = [
        sys.executable,
        "move_switch.py",
        selected_network["id"],
        selected_template["id"],
        org_id,
    ]
    if not autobind:
        cmd.append("--no-autobind")
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.append("--execute")
    if selected_all_networks:
        cmd.append("--all-in-template")

    render_context(selected_org, selected_network, selected_template, selected_all_networks)
    print()
    print(f"{status_chip('READY', True)} {bc.OKBLUE}Launching command{bc.ENDC}")
    print(f"{bc.LightCyan}{' '.join(cmd)}{bc.ENDC}")
    print()
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    move_menu()
