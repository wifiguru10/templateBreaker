# templateBreaker
## Meraki Template Tools
  These scripts allow you to unBind a MX template from a templated network WHILE it's in production with minimal impact to client traffic. 
  
  The script creates a new network with identical settings (addresses, ports, firewall, trafficshapping, autoVPN, etc) as the templated network and moves the hardware into the non-Templated network preserving the original. It'll also ensure the firmware matches on the destination network so your MX/Z3 device will have minimal client impact. In testing, the local outtage wasn't noticable and the autoVPN outtage was <20seconds. 
 
  **./unbind.py** \<networkID>  -   This unbinds a network where the networkID is a network that is currently BOUND to a template. (MX currently)
  
  **./rebind.py** \<networkID>  -   This reverses the unbind script, where the networkID is the networkID of the templated network (same used in unbinding) (MX)
  
  **./move.py** \<networkID> \<target_TemplateID>  \<destination_OrgID>-   This moves a network from one template, into another template. Preserving all settings. (MX/MS/MR/MV/MG) **NOW SUPPORTS CROSS-ORG and NAMED SEARCH**
  
    **Named example:** ./move.py "Test Network" "Template-A" "ProductionORG_1"


# templateBreaker for Switching (Catalyst + MS)

  **./move_switch.py** \<networkID or name> \<target_TemplateID or name> \<destination_OrgID or name optional> - Moves a switch network to a destination switch template by:
  1. Unbinding with `retainConfigs=True` (keeps current config)
  2. Binding to target template with `autoBind=True` by default (rebinds switches to matching switch profiles by model)
  3. Capturing switch port config snapshots before/after each network move and printing ASCII diff tables
  4. Reporting per-network and average success-rate percentages for port/settings parity
  
  Use `--all-in-template` to move **all switch networks bound to the source network's current template**.
  Dry-run is the default mode. Use `--execute` to make changes.

    **Example:** ./move_switch.py "Branch-001" "Switch-Template-New" "Production ORG"

  **Run first:** `./create_keys.py`  
  This stores/caches your Meraki API key locally so future commands (including `move_menu.py`) do not require manual API key entry each run.
  
  **./move_menu.py** - Text menu wrapper for `move_switch.py`:
  1. Select organization
  2. Select single switch network or `[ALL NETWORKS]` then choose source template (`--all-in-template` mode)
  3. Select destination switch template
  4. Optional prompts for `autoBind` and execution mode (dry-run default)


  
![2026-03-03_12-58-28 (2)](https://github.com/user-attachments/assets/5f2ceec6-3267-49cf-abc5-3d20ffbc77a0)
