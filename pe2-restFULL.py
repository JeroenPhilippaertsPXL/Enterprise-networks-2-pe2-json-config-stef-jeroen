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
    Bepaal het management IP van een device.
    - Switches: management_ip veld (bv. 172.16.9.133/28 → 172.16.9.133)
    - Routers: zoek de subinterface met encapsulation_dot1q: 10 (management VLAN)
    """
    mgmt = dev.get("management_ip")
    if mgmt:
        return mgmt.split("/")[0]

    if dev.get("type") == "router":
        # Zoek subinterface met encapsulation_dot1q = 10 (management VLAN)
        for iface_name, iface_settings in dev.get("interfaces", {}).items():
            if iface_settings.get("encapsulation_dot1q") == 10 and "ip_address" in iface_settings:
                return iface_settings["ip_address"]

    return None


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
        print(f"  ❌ [RESTCONF {method}] {url} → ERROR {err}")
        return None

    if r.status_code not in (200, 201, 204):
        print(f"  ❌ [RESTCONF {method}] {url} → HTTP {r.status_code}: {r.text[:200]}")
    else:
        print(f"  ✅ [RESTCONF {method}] {url} → OK ({r.status_code})")

    return r


# ---------------------------------------------------------
# GLOBAL / NATIVE CONFIG (hostname, banner, enable, lines, ssh, user)
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
        native["banner"] = {
            "motd": {
                "banner": device["banner_motd"]
            }
        }

    if "console" in device:
        native.setdefault("line", {})
        native["line"].setdefault("console", {"console": [{"first": 0}]})
        native["line"]["console"]["console"][0]["password"] = {
            "password": device["console"]["password"]
        }
        native["line"]["console"]["console"][0]["login"] = {}

    if "vty" in device:
        v = device["vty"]
        native.setdefault("line", {})
        first, last = str(v["lines"]).split()
        native["line"].setdefault("vty", {"vty": [{"first": int(first), "last": int(last)}]})
        vty0 = native["line"]["vty"]["vty"][0]

        if "password" in v:
            vty0["password"] = {"password": v["password"]}
        if v.get("login_local"):
            vty0["login"] = {"local": {}}
        if "transport_input" in v:
            vty0["transport"] = {"input": v["transport_input"]}
        if "access_class" in v:
            vty0["access-class"] = {"in": v["access_class"]}

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

    if not native:
        return

    payload = {"Cisco-IOS-XE-native:native": native}
    restconf_request("PATCH", url, auth, payload)


# ---------------------------------------------------------
# VLANs (switches)
# ---------------------------------------------------------
def push_vlans(device, auth):
    if device.get("type") != "switch" or "vlans" not in device:
        return

    for vlan in device["vlans"]:
        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-vlan:vlan/configuration/vlan-list={vlan['id']}"
        )
        payload = {
            "Cisco-IOS-XE-vlan:vlan-list": {
                "id": vlan["id"],
                "name": vlan["name"]
            }
        }
        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L3 INTERFACES / SVI / SUBINTERFACES (ietf-interfaces)
# ---------------------------------------------------------
def push_interface_l3(device, name, settings, auth):
    url = (
        f"https://{device['host']}/restconf/data/"
        f"ietf-interfaces:interfaces/interface={name}"
    )

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
            "address": [
                {
                    "ip": settings["ip_address"],
                    "netmask": settings["subnet_mask"]
                }
            ]
        }

    restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# L2 INTERFACES (switchport, trunk, port-channel) via native
# ---------------------------------------------------------
def push_interface_l2_native(device, name, settings, auth):
    if name.startswith("Gig"):
        if_type = "GigabitEthernet"
        if_name = name.replace("Gig", "")
    elif name.startswith("Port-channel"):
        if_type = "Port-channel"
        if_name = name.replace("Port-channel", "")
    else:
        return

    url = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}"
    )

    intf = {if_type: [{"name": if_name}]}
    obj = intf[if_type][0]

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
        obj.setdefault("channel-group", {})
        obj["channel-group"]["number"] = settings["channel_group"]
        obj["channel-group"]["mode"] = settings.get("channel_mode", "active")

    payload = {"Cisco-IOS-XE-native:interface": intf}
    restconf_request("PATCH", url, auth, payload)


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

        # Bepaal interface type en naam voor native pad
        if name.startswith("Gig"):
            if_type = "GigabitEthernet"
            if_name = name.replace("Gig", "")
        else:
            continue

        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-native:native/interface/{if_type}={if_name}"
        )

        payload = {
            f"Cisco-IOS-XE-native:{if_type}": [
                {
                    "name": if_name,
                    "standby": {
                        "standby-list": [
                            {
                                "group-number": s["group"],
                                "ip": {"address": s["ip"]},
                                "priority": s["priority"],
                                "preempt": {} if s.get("preempt", False) else None
                            }
                        ]
                    }
                }
            ]
        }

        # Verwijder None waarden
        standby_entry = payload[f"Cisco-IOS-XE-native:{if_type}"][0]["standby"]["standby-list"][0]
        if standby_entry["preempt"] is None:
            del standby_entry["preempt"]

        restconf_request("PATCH", url, auth, payload)


# ---------------------------------------------------------
# OSPF — via native YANG (IOS-XE 17.03 compatibel)
# ---------------------------------------------------------
def push_ospf(device, auth):
    if "ospf" not in device:
        return
    ospf = device["ospf"]

    url = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-native:native/router/ospf={ospf['process_id']}"
    )

    network_list = []
    for n in ospf["networks"]:
        network_list.append({
            "ip": n["network"],
            "mask": n["wildcard"],
            "area": n["area"]
        })

    payload = {
        "Cisco-IOS-XE-ospf:ospf": [
            {
                "id": ospf["process_id"],
                "router-id": ospf.get("router_id"),
                "passive-interface": ospf.get("passive_interfaces", []),
                "network": network_list
            }
        ]
    }

    restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# ACLs — via native YANG (IOS-XE 17.03 compatibel)
# ---------------------------------------------------------
def push_acls(device, auth):
    if "acls" not in device:
        return
    acls = device["acls"]

    for acl in acls.get("extended", []):
        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-native:native/ip/access-list/extended={acl['name']}"
        )

        ace_list = []
        seq = 10
        for e in acl["entries"]:
            ace = {
                "sequence": seq,
                "action": e["action"],
                "protocol": e["protocol"]
            }

            src = e["source"]
            if src == "any":
                ace["source-prefix"] = "any"
            elif src.startswith("host "):
                ace["source-prefix"] = "host"
                ace["source-ip"] = src.replace("host ", "")
            else:
                parts = src.split()
                ace["source-ip"] = parts[0]
                ace["source-wildcard"] = parts[1] if len(parts) > 1 else "0.0.0.0"

            dst = e["destination"]
            if dst == "any":
                ace["dest-prefix"] = "any"
            elif dst.startswith("host "):
                ace["dest-prefix"] = "host"
                ace["dest-ip"] = dst.replace("host ", "")
            else:
                parts = dst.split()
                ace["dest-ip"] = parts[0]
                ace["dest-wildcard"] = parts[1] if len(parts) > 1 else "0.0.0.0"

            if "dest_port" in e:
                ace["dst-eq"] = e["dest_port"]

            ace_list.append(ace)
            seq += 10

        payload = {
            "Cisco-IOS-XE-acl:extended": [
                {
                    "name": acl["name"],
                    "access-list-seq-rule": ace_list
                }
            ]
        }

        restconf_request("PUT", url, auth, payload)

    if "nat_acl" in acls:
        nat = acls["nat_acl"]
        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-native:native/ip/access-list/standard={nat['number']}"
        )

        ace_list = []
        seq = 10
        for e in nat["entries"]:
            parts = e["source"].split()
            ace = {
                "sequence": seq,
                "action": "permit",
                "source-ip": parts[0],
                "source-wildcard": parts[1] if len(parts) > 1 else "0.0.0.0"
            }
            ace_list.append(ace)
            seq += 10

        payload = {
            "Cisco-IOS-XE-acl:standard": [
                {
                    "name": str(nat["number"]),
                    "access-list-seq-rule": ace_list
                }
            ]
        }

        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# NAT — via native YANG (IOS-XE 17.03 compatibel)
# ---------------------------------------------------------
def push_nat(device, auth):
    if "nat" not in device:
        return

    nat = device["nat"]
    pool = nat["pool"]

    # NAT pool
    url_pool = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-native:native/ip/nat/pool={pool['name']}"
    )

    payload_pool = {
        "Cisco-IOS-XE-nat:pool": [
            {
                "id": pool["name"],
                "start-address": pool["start_ip"],
                "end-address": pool["end_ip"],
                "netmask": pool["netmask"]
            }
        ]
    }

    restconf_request("PUT", url_pool, auth, payload_pool)

    # NAT inside source
    inside = nat["inside_source"]
    url_inside = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-native:native/ip/nat/inside/source/list={inside['acl']}/pool/{inside['pool']}"
    )

    payload_inside = {
        "Cisco-IOS-XE-nat:pool": [
            {
                "id": inside["pool"],
                "overload": inside.get("overload", False)
            }
        ]
    }

    restconf_request("PUT", url_inside, auth, payload_inside)


# ---------------------------------------------------------
# DHCP SERVICE TOGGLES
# ---------------------------------------------------------
def push_dhcp_service(device, auth):
    if "dhcp" not in device:
        return
    dhcp = device["dhcp"]

    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/ip"
    native_ip = {}

    if dhcp.get("service_dhcp") and not dhcp.get("no_service_dhcp"):
        native_ip["dhcp"] = {}

    if not native_ip:
        return

    payload = {"Cisco-IOS-XE-native:ip": native_ip}
    restconf_request("PATCH", url, auth, payload)


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
            print(f"Skipping device {dev.get('name', '<unknown>')} — no usable IP address was found.")
            continue

        dev["host"] = host

        ssh = dev.get("ssh")
        if not ssh or "username" not in ssh or "secret" not in ssh:
            print(f"Skipping device {dev.get('name', '<unknown>')} — SSH credentials missing.")
            continue

        auth = (ssh["username"], ssh["secret"])
        print(f"\n{'='*60}")
        print(f"Device: {dev.get('name', '<unknown>')} ({dev.get('type', '<unknown>')}) @ {host}")
        print(f"{'='*60}")

        push_global_native(dev, auth)
        push_vlans(dev, auth)
        push_interfaces(dev, auth)
        push_hsrp(dev, auth)
        push_ospf(dev, auth)
        push_acls(dev, auth)
        push_nat(dev, auth)
        push_dhcp_service(dev, auth)

    print("\n✅ Deployment voltooid.")


if __name__ == "__main__":
    main()
