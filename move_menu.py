#!/usr/bin/python3

import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

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


def group_networks_by_template(networks: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for network in networks:
        template_id = network.get("configTemplateId")
        if not template_id:
            continue
        grouped.setdefault(template_id, []).append(network)
    return grouped


def select_from_list(title: str, items: List[Tuple[str, str]]) -> Optional[int]:
    print()
    print(f"{bc.HEADER}{bc.BOLD}{title}{bc.ENDC}")
    for idx, (label, value) in enumerate(items, start=1):
        print(f"{bc.OKBLUE}{idx}.{bc.ENDC} {bc.OKGREEN}{label}{bc.ENDC} [{bc.WARNING}{value}{bc.ENDC}]")
    print(f"{bc.FAIL}0.{bc.ENDC} {bc.WARNING}Cancel{bc.ENDC}")

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
        print(f"{bc.FAIL}Enter a value between 0 and {len(items)}.{bc.ENDC}")


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
    print(f"{bc.HEADER}{bc.BOLD}Switch Template Move Menu{bc.ENDC}")
    org_whitelist = load_org_whitelist()
    orgs = get_candidate_orgs(org_whitelist)
    if not orgs:
        print(f"{bc.FAIL}ERROR:{bc.OKGREEN} no organizations available from API key/whitelist.{bc.ENDC}")
        return

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

    template_by_id = {t["id"]: t for t in templates}
    network_items = []
    for network in networks:
        current_template = template_by_id.get(network["configTemplateId"])
        current_template_name = current_template["name"] if current_template else "Unknown"
        label = f"{network['name']} (current template: {current_template_name})"
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
            source_template_items.append(
                (f"{source_template['name']} ({bound_count} bound networks)", source_template["id"])
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

    template_items = [(template["name"], template["id"]) for template in destination_templates]
    template_index = select_from_list("Select destination switch template:", template_items)
    if template_index is None:
        print(f"{bc.WARNING}Cancelled.{bc.ENDC}")
        return
    selected_template = destination_templates[template_index]

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

    print()
    print(f"{bc.HEADER}{bc.BOLD}Executing{bc.ENDC}")
    print(f"{bc.LightCyan}{' '.join(cmd)}{bc.ENDC}")
    print()
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    move_menu()
