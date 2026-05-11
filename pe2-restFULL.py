import argparse
import json
import os
import requests
import urllib3
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------
GITHUB_JSON_URL = "https://raw.githubusercontent.com/JeroenPhilippaertsPXL/Enterprise-networks-2-pe2-json-config-stef-jeroen/main/config.json"
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------
def load_json_from_github(url):
    print(f"Downloading JSON config from GitHub: {url}")
    r = requests.get(url, timeout=10, verify=False)
    r.raise_for_status()
    print("JSON successfully downloaded and parsed.")
    return r.json()


def load_json_from_file(path):
    print(f"Loading JSON config from local file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(source=None):
    if source is None:
        if os.path.isfile(DEFAULT_CONFIG_FILE):
            source = DEFAULT_CONFIG_FILE
        else:
            source = GITHUB_JSON_URL
    if source.startswith(("http://", "https://")):
        return load_json_from_github(source)
    if os.path.isfile(source):
        return load_json_from_file(source)
    raise FileNotFoundError(f"Config source not found: {source}")


def resolve_host(dev):
    mgmt = dev.get("management_ip")
    if mgmt:
        return mgmt.split("/")[0]
    if dev.get("type") == "router":
        for iface_settings in dev.get("interfaces", {}).values():
            if iface_settings.get("encapsulation_dot1q") == 10 and "ip_address" in iface_settings:
                return iface_settings["ip_address"]
    return None


def encode_key(key):
    """URL-encode een RESTCONF key waarde."""
    return quote(str(key), safe="")


def parse_if_type_name(name):
    """Zet interface naam om naar IOS-XE native type en naam."""
    if name.startswith("Gig"):
        return "GigabitEthernet", name.replace("Gig", "")
    elif name.startswith("Port-channel"):
        return "Port-channel", name.replace("Port-channel", "")
    elif name.startswith("Vlan"):
        return "Vlan", name.replace("Vlan", "")
    return None, None


def restconf_request(method, url, auth, payload=None):
    headers = {
        "Content-Type": "application/yang-data+json",
        "Accept": "application/yang-data+json"
    }
    data = json.dumps(payload) if payload is not None else None
    try:
        r = requests.request(method, url, headers=headers, data=data,
                             auth=auth, verify=False, timeout=10)
    except requests.exceptions.RequestException as err:
        print(f"  ERROR [{method}] {url} -> {err}")
        return None
    if r.status_code not in (200, 201, 204):
        print(f"  FAIL  [{method}] {url} -> HTTP {r.status_code}: {r.text[:300]}")
    else:
        print(f"  OK    [{method}] {url} -> {r.status_code}")
    return r


# ---------------------------------------------------------
# GLOBAL CONFIG — afzonderlijke kleine requests
# ---------------------------------------------------------
def push_hostname(device, auth):
    if "hostname" not in device:
        return
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/hostname"
    restconf_request("PUT", url, auth, {"Cisco-IOS-XE-native:hostname": device["hostname"]})


def push_banner(device, auth):
    if "banner_motd" not in device:
        return
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/banner/motd"
    restconf_request("PUT", url, auth, {"Cisco-IOS-XE-native:motd": {"banner": device["banner_motd"]}})


def push_username(device, auth):
    ssh = device.get("ssh")
    if not ssh:
        return
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/username={encode_key(ssh['username'])}"
    payload = {
        "Cisco-IOS-XE-native:username": [{
            "name": ssh["username"],
            "privilege": 15,
            "secret": {"secret": ssh["secret"]}
        }]
    }
    restconf_request("PUT", url, auth, payload)


def push_ip_domain(device, auth):
    ssh = device.get("ssh")
    if not ssh or "domain_name" not in ssh:
        return
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/ip/domain"
    restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-native:domain": {"name": ssh["domain_name"]}})


def push_default_gateway(device, auth):
    if "ip_default_gateway" not in device:
        return
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/ip/default-gateway"
    restconf_request("PUT", url, auth, {"Cisco-IOS-XE-native:default-gateway": device["ip_default_gateway"]})


def push_global_native(device, auth):
    push_hostname(device, auth)
    push_banner(device, auth)
    push_username(device, auth)
    push_ip_domain(device, auth)
    push_default_gateway(device, auth)


# ---------------------------------------------------------
# VLANs (switches)
# ---------------------------------------------------------
def push_vlans(device, auth):
    if device.get("type") != "switch" or "vlans" not in device:
        return
    for vlan in device["vlans"]:
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/vlan/vlan-list={vlan['id']}")
        payload = {"Cisco-IOS-XE-vlan:vlan-list": {"id": vlan["id"], "name": vlan["name"]}}
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L3 INTERFACES — volledig via native YANG
# Fix: encapsulation + IP samen via native, niet via ietf-interfaces
# ---------------------------------------------------------
def push_interface_l3(device, name, settings, auth):
    if_type, if_name = parse_if_type_name(name)
    if not if_type:
        return

    url = (f"https://{device['host']}/restconf/data/"
           f"Cisco-IOS-XE-native:native/interface/{if_type}={encode_key(if_name)}")

    # Vlan gebruikt integer als naam
    obj_name = int(if_name) if if_type == "Vlan" else if_name
    obj = {"name": obj_name}

    # Encapsulation voor subinterfaces (bv. Gig0/0/0.10)
    if "encapsulation_dot1q" in settings:
        vlan_id = settings["encapsulation_dot1q"]
        obj["encapsulation"] = {"dot1Q": {"vlan-id": vlan_id}}

    # IP adres
    if "ip_address" in settings:
        obj["ip"] = {
            "address": {
                "primary": {
                    "address": settings["ip_address"],
                    "mask": settings["subnet_mask"]
                }
            }
        }

    # Helper address
    if "ip_helper_address" in settings:
        obj.setdefault("ip", {})
        obj["ip"]["helper-address"] = [{"address": settings["ip_helper_address"]}]

    # Shutdown
    if settings.get("shutdown", False):
        obj["shutdown"] = {}

    # Fix: payload key is Cisco-IOS-XE-native:{if_type}, niet Cisco-IOS-XE-native:interface
    payload = {f"Cisco-IOS-XE-native:{if_type}": [obj]}
    restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L2 INTERFACES — via native YANG
# Fix: payload key is Cisco-IOS-XE-native:{if_type}, niet Cisco-IOS-XE-native:interface
# ---------------------------------------------------------
def push_interface_l2_native(device, name, settings, auth):
    if_type, if_name = parse_if_type_name(name)
    if not if_type or if_type == "Vlan":
        return

    url = (f"https://{device['host']}/restconf/data/"
           f"Cisco-IOS-XE-native:native/interface/{if_type}={encode_key(if_name)}")

    obj = {"name": if_name}

    if "switchport_mode" in settings:
        obj.setdefault("Cisco-IOS-XE-switch:switchport", {})
        mode = settings["switchport_mode"]
        if mode == "access":
            obj["Cisco-IOS-XE-switch:switchport"]["mode"] = {"access": {}}
        elif mode == "trunk":
            obj["Cisco-IOS-XE-switch:switchport"]["mode"] = {"trunk": {}}

    if "access_vlan" in settings:
        obj.setdefault("Cisco-IOS-XE-switch:switchport", {})
        obj["Cisco-IOS-XE-switch:switchport"].setdefault("access", {})
        obj["Cisco-IOS-XE-switch:switchport"]["access"]["vlan"] = settings["access_vlan"]

    if "trunk_native_vlan" in settings:
        obj.setdefault("Cisco-IOS-XE-switch:switchport", {})
        obj["Cisco-IOS-XE-switch:switchport"].setdefault("trunk", {})
        obj["Cisco-IOS-XE-switch:switchport"]["trunk"]["native"] = settings["trunk_native_vlan"]

    if "channel_group" in settings:
        obj["Cisco-IOS-XE-etherchannel:channel-group"] = {
            "number": settings["channel_group"],
            "mode": settings.get("channel_mode", "active")
        }

    # Fix: gebruik if_type als key, niet "interface"
    payload = {f"Cisco-IOS-XE-native:{if_type}": [obj]}

    # Port-channel: PUT ipv PATCH want het bestaat nog niet
    method = "PUT" if if_type == "Port-channel" else "PATCH"
    restconf_request(method, url, auth, payload)


# ---------------------------------------------------------
# NAT INTERFACE RICHTING (inside / outside)
# ---------------------------------------------------------
def push_nat_interfaces(device, auth):
    if "interfaces" not in device:
        return
    for name, settings in device["interfaces"].items():
        if not (settings.get("nat_inside") or settings.get("nat_outside")):
            continue
        if_type, if_name = parse_if_type_name(name)
        if not if_type:
            continue
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/interface/{if_type}={encode_key(if_name)}/ip/nat")
        nat_payload = {}
        if settings.get("nat_outside"):
            nat_payload["outside"] = {}
        if settings.get("nat_inside"):
            nat_payload["inside"] = {}
        restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-nat:nat": nat_payload})


# ---------------------------------------------------------
# OSPF COST per interface
# Fix: subinterfaces moeten eerst bestaan via push_interface_l3
# ---------------------------------------------------------
def push_ospf_costs(device, auth):
    if "interfaces" not in device:
        return
    for name, settings in device["interfaces"].items():
        if "ospf_cost" not in settings:
            continue
        if_type, if_name = parse_if_type_name(name)
        if not if_type:
            continue
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/interface/{if_type}={encode_key(if_name)}/ip/ospf/cost")
        restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-ospf:cost": settings["ospf_cost"]})


# ---------------------------------------------------------
# INTERFACE DISPATCHER
# ---------------------------------------------------------
def push_interfaces(device, auth):
    if "interfaces" not in device:
        return
    for name, settings in device["interfaces"].items():
        if "ip_address" in settings or "encapsulation_dot1q" in settings:
            push_interface_l3(device, name, settings, auth)
        if device.get("type") == "switch":
            if any(k in settings for k in ["switchport_mode", "access_vlan", "trunk_native_vlan", "channel_group"]):
                push_interface_l2_native(device, name, settings, auth)


# ---------------------------------------------------------
# HSRP — native YANG met URL encoding
# ---------------------------------------------------------
def push_hsrp(device, auth):
    if "interfaces" not in device:
        return
    for name, settings in device["interfaces"].items():
        if "standby" not in settings:
            continue
        s = settings["standby"]
        if_type, if_name = parse_if_type_name(name)
        if not if_type:
            continue
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/interface/{if_type}={encode_key(if_name)}")
        standby_entry = {
            "group-number": s["group"],
            "ip": {"address": s["ip"]},
            "priority": s["priority"]
        }
        if s.get("preempt", False):
            standby_entry["preempt"] = {}
        # Fix: gebruik if_type als key
        payload = {f"Cisco-IOS-XE-native:{if_type}": [{
            "name": if_name,
            "standby": {"standby-list": [standby_entry]}
        }]}
        restconf_request("PATCH", url, auth, payload)


# ---------------------------------------------------------
# OSPF — native YANG
# Fix: single object ipv list
# ---------------------------------------------------------
def push_ospf(device, auth):
    if "ospf" not in device:
        return
    ospf = device["ospf"]
    url = (f"https://{device['host']}/restconf/data/"
           f"Cisco-IOS-XE-native:native/router/ospf={ospf['process_id']}")
    process = {
        "id": ospf["process_id"],
        "network": [
            {"ip": n["network"], "mask": n["wildcard"], "area": n["area"]}
            for n in ospf["networks"]
        ]
    }
    if "router_id" in ospf:
        process["router-id"] = ospf["router_id"]
    if "passive_interfaces" in ospf:
        process["passive-interface"] = ospf["passive_interfaces"]

    # Fix: single object ipv list
    restconf_request("PUT", url, auth, {"Cisco-IOS-XE-ospf:ospf": process})


# ---------------------------------------------------------
# ACLs — native YANG
# Fix: action zit in ace-rule container voor extended ACL
# Fix: permit/deny container voor standard ACL
# ---------------------------------------------------------
def push_acls(device, auth):
    if "acls" not in device:
        return
    acls = device["acls"]

    # Extended ACLs
    for acl in acls.get("extended", []):
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/extended={encode_key(acl['name'])}")

        ace_list = []
        for i, e in enumerate(acl["entries"]):
            seq = str((i + 1) * 10)
            ace_rule = {
                "action": e["action"],
                "protocol": e["protocol"]
            }

            # Source
            src = e.get("source", "any")
            if src == "any":
                ace_rule["any"] = {}
            elif src.startswith("host "):
                ace_rule["host"] = src.replace("host ", "")
            else:
                parts = src.split()
                ace_rule["ipv4-address"] = parts[0]
                if len(parts) > 1:
                    ace_rule["mask"] = parts[1]

            # Destination
            dst = e.get("destination", "any")
            if dst == "any":
                ace_rule["dest-any"] = {}
            elif dst.startswith("host "):
                ace_rule["dest-host"] = dst.replace("host ", "")
            else:
                parts = dst.split()
                ace_rule["dest-ipv4-address"] = parts[0]
                if len(parts) > 1:
                    ace_rule["dest-mask"] = parts[1]

            if "dest_port" in e:
                ace_rule["dst-eq"] = str(e["dest_port"])

            ace_list.append({"sequence": seq, "ace-rule": ace_rule})

        payload = {
            "Cisco-IOS-XE-acl:extended": [{
                "name": acl["name"],
                "access-list-seq-rule": ace_list
            }]
        }
        restconf_request("PUT", url, auth, payload)

    # Standard ACLs
    for acl in acls.get("standard", []):
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/standard={encode_key(acl['name'])}")

        ace_list = []
        for i, e in enumerate(acl["entries"]):
            seq = str((i + 1) * 10)
            action = e["action"]
            src = e.get("source", "any")

            std_ace = {}
            if src == "any":
                std_ace["any"] = {}
            elif src.startswith("host "):
                std_ace["host"] = src.replace("host ", "")
            else:
                parts = src.split()
                std_ace["ipv4-prefix"] = parts[0]
                if len(parts) > 1:
                    std_ace["mask"] = parts[1]

            ace_list.append({"sequence": seq, action: {"std-ace": std_ace}})

        payload = {
            "Cisco-IOS-XE-acl:standard": [{
                "name": str(acl["name"]),
                "access-list-seq-rule": ace_list
            }]
        }
        restconf_request("PUT", url, auth, payload)

    # NAT ACL (standard numbered)
    if "nat_acl" in acls:
        nat = acls["nat_acl"]
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/standard={nat['number']}")

        ace_list = []
        for i, e in enumerate(nat["entries"]):
            seq = str((i + 1) * 10)
            src = e.get("source", "any")
            std_ace = {}
            if src == "any":
                std_ace["any"] = {}
            else:
                parts = src.split()
                std_ace["ipv4-prefix"] = parts[0]
                if len(parts) > 1:
                    std_ace["mask"] = parts[1]
            ace_list.append({"sequence": seq, "permit": {"std-ace": std_ace}})

        payload = {
            "Cisco-IOS-XE-acl:standard": [{
                "name": str(nat["number"]),
                "access-list-seq-rule": ace_list
            }]
        }
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# NAT POOL + INSIDE SOURCE — native YANG
# Fix: correct inside source pad
# ---------------------------------------------------------
def push_nat(device, auth):
    if "nat" not in device:
        return
    nat = device["nat"]
    pool = nat["pool"]

    # NAT pool
    url_pool = (f"https://{device['host']}/restconf/data/"
                f"Cisco-IOS-XE-native:native/ip/nat/pool={encode_key(pool['name'])}")
    payload_pool = {
        "Cisco-IOS-XE-nat:pool": [{
            "id": pool["name"],
            "start-address": pool["start_ip"],
            "end-address": pool["end_ip"],
            "netmask": pool["netmask"]
        }]
    }
    restconf_request("PUT", url_pool, auth, payload_pool)

    # NAT inside source list — correct pad voor IOS-XE native
    inside = nat["inside_source"]
    url_inside = (f"https://{device['host']}/restconf/data/"
                  f"Cisco-IOS-XE-native:native/ip/nat/inside/source/list={inside['acl']}")
    entry = {
        "id": inside["acl"],
        "pool": {"pool-name": inside["pool"]}
    }
    if inside.get("overload"):
        entry["overload"] = {}
    payload_inside = {"Cisco-IOS-XE-nat:list": [entry]}
    restconf_request("PUT", url_inside, auth, payload_inside)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Push RESTCONF config to Cisco IOS-XE devices")
    parser.add_argument("--config-file", help="Local JSON config file path")
    parser.add_argument("--config-url", help="Remote JSON config URL")
    args = parser.parse_args()

    try:
        data = load_config(args.config_url or args.config_file)
    except Exception as err:
        print(f"Kon config niet laden: {err}")
        return

    for dev in data.get("devices", []):
        host = resolve_host(dev)
        if not host:
            print(f"Skipping {dev.get('name', '<unknown>')} — no usable IP found.")
            continue

        dev["host"] = host
        ssh = dev.get("ssh")
        if not ssh or "username" not in ssh or "secret" not in ssh:
            print(f"Skipping {dev.get('name', '<unknown>')} — SSH credentials missing.")
            continue

        auth = (ssh["username"], ssh["secret"])
        print(f"\n{'='*60}")
        print(f"Device: {dev.get('name')} ({dev.get('type')}) @ {host}")
        print(f"{'='*60}")

        push_global_native(dev, auth)
        push_vlans(dev, auth)
        push_interfaces(dev, auth)
        push_nat_interfaces(dev, auth)
        push_ospf_costs(dev, auth)
        push_hsrp(dev, auth)
        push_ospf(dev, auth)
        push_acls(dev, auth)
        push_nat(dev, auth)

    print("\nDeployment voltooid.")


if __name__ == "__main__":
    main()
