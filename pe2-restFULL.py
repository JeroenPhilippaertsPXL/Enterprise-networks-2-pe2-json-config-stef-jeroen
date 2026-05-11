import argparse
import json
import os
import requests
import urllib3

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
    """
    Bepaal het management IP:
    - Switches: management_ip veld (bv. 172.16.9.133/28 -> 172.16.9.133)
    - Routers: subinterface met encapsulation_dot1q = 10 (management VLAN)
    """
    mgmt = dev.get("management_ip")
    if mgmt:
        return mgmt.split("/")[0]
    if dev.get("type") == "router":
        for iface_settings in dev.get("interfaces", {}).values():
            if iface_settings.get("encapsulation_dot1q") == 10 and "ip_address" in iface_settings:
                return iface_settings["ip_address"]
    return None


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
        print(f"  FAIL  [{method}] {url} -> HTTP {r.status_code}: {r.text[:200]}")
    else:
        print(f"  OK    [{method}] {url} -> {r.status_code}")
    return r


# ---------------------------------------------------------
# GLOBAL / NATIVE CONFIG
# ---------------------------------------------------------
def push_global_native(device, auth):
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native"
    native = {}

    if "hostname" in device:
        native["hostname"] = device["hostname"]
    if "enable_secret" in device:
        native["enable-secret"] = device["enable_secret"]
    if device.get("service_password_encryption"):
        native["service"] = {"password-encryption": {}}
    if "banner_motd" in device:
        native["banner"] = {"motd": {"banner": device["banner_motd"]}}

    if "console" in device:
        native.setdefault("line", {})
        native["line"]["console"] = {
            "console": [{
                "first": 0,
                "password": {"password": device["console"]["password"]},
                "login": {}
            }]
        }

    if "vty" in device:
        v = device["vty"]
        first, last = str(v["lines"]).split()
        vty_entry = {"first": int(first), "last": int(last)}
        if "password" in v:
            vty_entry["password"] = {"password": v["password"]}
        if v.get("login_local"):
            vty_entry["login"] = {"local": {}}
        if "transport_input" in v:
            vty_entry["transport"] = {"input": v["transport_input"]}
        if "access_class" in v:
            vty_entry["access-class"] = {"in": v["access_class"]}
        native.setdefault("line", {})
        native["line"]["vty"] = {"vty": [vty_entry]}

    if "ssh" in device:
        ssh = device["ssh"]
        native.setdefault("username", [])
        native["username"].append({
            "name": ssh["username"],
            "secret": {"secret": ssh["secret"]}
        })
        native.setdefault("ip", {})
        native["ip"].setdefault("domain", {})
        native["ip"]["domain"]["name"] = ssh["domain_name"]
        native["ip"].setdefault("ssh", {})
        native["ip"]["ssh"]["version"] = ssh["version"]

    if "ip_default_gateway" in device:
        native.setdefault("ip", {})
        native["ip"]["default-gateway"] = device["ip_default_gateway"]

    if not native:
        return
    restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-native:native": native})


# ---------------------------------------------------------
# VLANs (switches)
# ---------------------------------------------------------
def push_vlans(device, auth):
    if device.get("type") != "switch" or "vlans" not in device:
        return
    for vlan in device["vlans"]:
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-vlan:vlan/configuration/vlan-list={vlan['id']}")
        payload = {"Cisco-IOS-XE-vlan:vlan-list": {"id": vlan["id"], "name": vlan["name"]}}
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L3 INTERFACES / SVI / SUBINTERFACES
# ---------------------------------------------------------
def push_interface_l3(device, name, settings, auth):
    url = (f"https://{device['host']}/restconf/data/"
           f"ietf-interfaces:interfaces/interface={name}")
    enabled = not settings.get("shutdown", False)
    payload = {
        "ietf-interfaces:interface": {
            "name": name,
            "type": "iana-if-type:ethernetCsmacd",
            "enabled": enabled
        }
    }
    if "ip_address" in settings:
        payload["ietf-interfaces:interface"]["ietf-ip:ipv4"] = {
            "address": [{"ip": settings["ip_address"], "netmask": settings["subnet_mask"]}]
        }
    restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L2 INTERFACES (switchport, trunk, port-channel) via native
# ---------------------------------------------------------
def push_interface_l2_native(device, name, settings, auth):
    if_type, if_name = parse_if_type_name(name)
    if not if_type or if_type == "Vlan":
        return

    url = (f"https://{device['host']}/restconf/data/"
           f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}")

    obj = {"name": if_name}

    if "switchport_mode" in settings:
        obj.setdefault("switchport", {})
        mode = settings["switchport_mode"]
        if mode == "access":
            obj["switchport"]["mode"] = {"access": {}}
        elif mode == "trunk":
            obj["switchport"]["mode"] = {"trunk": {}}

    if "access_vlan" in settings:
        obj.setdefault("switchport", {})
        obj["switchport"].setdefault("access", {})
        obj["switchport"]["access"]["vlan"] = settings["access_vlan"]

    if "trunk_native_vlan" in settings:
        obj.setdefault("switchport", {})
        obj["switchport"].setdefault("trunk", {})
        obj["switchport"]["trunk"]["native"] = settings["trunk_native_vlan"]

    if "channel_group" in settings:
        obj["channel-group"] = {
            "number": settings["channel_group"],
            "mode": settings.get("channel_mode", "active")
        }

    payload = {"Cisco-IOS-XE-native:interface": {if_type: [obj]}}
    restconf_request("PATCH", url, auth, payload)


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
               f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}/ip/nat")
        nat_payload = {}
        if settings.get("nat_outside"):
            nat_payload["outside"] = {}
        if settings.get("nat_inside"):
            nat_payload["inside"] = {}
        restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-nat:nat": nat_payload})


# ---------------------------------------------------------
# IP HELPER ADDRESS (DHCP relay)
# ---------------------------------------------------------
def push_helper_addresses(device, auth):
    if "interfaces" not in device:
        return
    for name, settings in device["interfaces"].items():
        if "ip_helper_address" not in settings:
            continue
        if_type, if_name = parse_if_type_name(name)
        if not if_type:
            continue
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}/ip/helper-address")
        payload = {"Cisco-IOS-XE-native:helper-address": [{"ip": settings["ip_helper_address"]}]}
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# OSPF COST per interface
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
               f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}/ip/ospf/cost")
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
# HSRP — via native YANG (IOS-XE 17.03 compatibel)
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
               f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}")

        standby_entry = {
            "group-number": s["group"],
            "ip": {"address": s["ip"]},
            "priority": s["priority"]
        }
        if s.get("preempt", False):
            standby_entry["preempt"] = {}

        payload = {
            f"Cisco-IOS-XE-native:{if_type}": [{
                "name": if_name,
                "standby": {"standby-list": [standby_entry]}
            }]
        }
        restconf_request("PATCH", url, auth, payload)


# ---------------------------------------------------------
# OSPF — via native YANG (IOS-XE 17.03 compatibel)
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
    restconf_request("PUT", url, auth, {"Cisco-IOS-XE-ospf:ospf": [process]})


# ---------------------------------------------------------
# ACLs — via native YANG (IOS-XE 17.03 compatibel)
# ---------------------------------------------------------
def push_acls(device, auth):
    if "acls" not in device:
        return
    acls = device["acls"]

    def build_ace(e, seq):
        ace = {"sequence": seq, "action": e["action"]}
        if "protocol" in e:
            ace["protocol"] = e["protocol"]
        for field, src_key, wc_key, pfx_key, ip_key in [
            ("source", "source-ip", "source-wildcard", "source-prefix", "source-ip"),
            ("destination", "dest-ip", "dest-wildcard", "dest-prefix", "dest-ip"),
        ]:
            if field not in e:
                continue
            val = e[field]
            if val == "any":
                ace[pfx_key] = "any"
            elif val.startswith("host "):
                ace[pfx_key] = "host"
                ace[ip_key] = val.replace("host ", "")
            else:
                parts = val.split()
                ace[ip_key] = parts[0]
                if len(parts) > 1:
                    ace[wc_key] = parts[1]
        if "dest_port" in e:
            ace["dst-eq"] = e["dest_port"]
        return ace

    for acl in acls.get("extended", []):
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/extended={acl['name']}")
        ace_list = [build_ace(e, (i+1)*10) for i, e in enumerate(acl["entries"])]
        payload = {"Cisco-IOS-XE-acl:extended": [{"name": acl["name"], "access-list-seq-rule": ace_list}]}
        restconf_request("PUT", url, auth, payload)

    for acl in acls.get("standard", []):
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/standard={acl['name']}")
        ace_list = [build_ace(e, (i+1)*10) for i, e in enumerate(acl["entries"])]
        payload = {"Cisco-IOS-XE-acl:standard": [{"name": acl["name"], "access-list-seq-rule": ace_list}]}
        restconf_request("PUT", url, auth, payload)

    if "nat_acl" in acls:
        nat = acls["nat_acl"]
        url = (f"https://{device['host']}/restconf/data/"
               f"Cisco-IOS-XE-native:native/ip/access-list/standard={nat['number']}")
        ace_list = [build_ace(e, (i+1)*10) for i, e in enumerate(nat["entries"])]
        payload = {"Cisco-IOS-XE-acl:standard": [{"name": str(nat["number"]), "access-list-seq-rule": ace_list}]}
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# NAT POOL + INSIDE SOURCE — via native YANG
# ---------------------------------------------------------
def push_nat(device, auth):
    if "nat" not in device:
        return
    nat = device["nat"]
    pool = nat["pool"]

    url_pool = (f"https://{device['host']}/restconf/data/"
                f"Cisco-IOS-XE-native:native/ip/nat/pool={pool['name']}")
    payload_pool = {
        "Cisco-IOS-XE-nat:pool": [{
            "id": pool["name"],
            "start-address": pool["start_ip"],
            "end-address": pool["end_ip"],
            "netmask": pool["netmask"]
        }]
    }
    restconf_request("PUT", url_pool, auth, payload_pool)

    inside = nat["inside_source"]
    url_inside = (f"https://{device['host']}/restconf/data/"
                  f"Cisco-IOS-XE-native:native/ip/nat/inside/source/list={inside['acl']}/pool/{inside['pool']}")
    payload_inside = {
        "Cisco-IOS-XE-nat:pool": [{"id": inside["pool"], "overload": inside.get("overload", False)}]
    }
    restconf_request("PUT", url_inside, auth, payload_inside)


# ---------------------------------------------------------
# SNMP — via native YANG
# ---------------------------------------------------------
def push_snmp(device, auth):
    if "snmp" not in device:
        return
    snmp = device["snmp"]
    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/snmp-server"

    communities = []
    for c in snmp.get("communities", []):
        entry = {
            "name": c["name"],
            "permission": "read-only" if c["mode"].upper() == "RO" else "read-write"
        }
        if "acl" in c:
            entry["access-list-name"] = c["acl"]
        communities.append(entry)

    hosts = []
    for h in snmp.get("hosts", []):
        hosts.append({
            "ip-address": h["host"],
            "version": {"version-2c": {"community": h["community"]}}
        })

    snmp_payload = {}
    if communities:
        snmp_payload["Cisco-IOS-XE-snmp:community"] = communities
    if hosts:
        snmp_payload["Cisco-IOS-XE-snmp:host"] = hosts

    if snmp_payload:
        restconf_request("PATCH", url, auth, {"Cisco-IOS-XE-native:snmp-server": snmp_payload})


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
        push_helper_addresses(dev, auth)
        push_ospf_costs(dev, auth)
        push_hsrp(dev, auth)
        push_ospf(dev, auth)
        push_acls(dev, auth)
        push_nat(dev, auth)
        push_snmp(dev, auth)

    print("\nDeployment voltooid.")


if __name__ == "__main__":
    main()
