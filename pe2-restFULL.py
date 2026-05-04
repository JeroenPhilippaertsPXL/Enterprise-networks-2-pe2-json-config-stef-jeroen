import argparse
import json
import os
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------
GITHUB_JSON_URL = "https://raw.githubusercontent.com/<user>/<repo>/main/lab_config.json"
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "lab_config.json")


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
        print(f"[RESTCONF {method}] {url} -> ERROR {err}")
        return None

    if r.status_code not in (200, 201, 204):
        print(f"[RESTCONF {method}] {url} -> {r.status_code} {r.text}")
    else:
        print(f"[RESTCONF {method}] {url} -> OK")

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
            vty0["access-class"] = {
                "in": v["access_class"]
            }

    if "ssh" in device:
        ssh = device["ssh"]
        native.setdefault("username", [])
        native["username"].append({
            "name": ssh["username"],
            "secret": {
                "secret": ssh["secret"]
            }
        })
        native.setdefault("ip", {})
        native["ip"].setdefault("domain", {})
        native["ip"]["domain"]["name"] = ssh["domain_name"]

        native["ip"].setdefault("ssh", {})
        native["ip"]["ssh"]["version"] = ssh["version"]

    if not native:
        return

    payload = {
        "Cisco-IOS-XE-native:native": native
    }

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

    intf = {
        if_type: [
            {
                "name": if_name
            }
        ]
    }

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

    payload = {
        "Cisco-IOS-XE-native:interface": intf
    }

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
# HSRP
# ---------------------------------------------------------
def push_hsrp(device, auth):
    if "interfaces" not in device:
        return

    for name, settings in device["interfaces"].items():
        if "standby" not in settings:
            continue
        s = settings["standby"]

        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-hsrp:hsrp/hsrp-interface={name}/ipv4/hsrp-group={s['group']}"
        )

        payload = {
            "Cisco-IOS-XE-hsrp:hsrp-group": {
                "group-number": s["group"],
                "priority": s["priority"],
                "preempt": s.get("preempt", False),
                "virtual-ip": s["ip"]
            }
        }

        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# OSPF
# ---------------------------------------------------------
def push_ospf(device, auth):
    if "ospf" not in device:
        return
    ospf = device["ospf"]

    url = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-ospf:ospf/processes/process={ospf['process_id']}"
    )

    payload = {
        "Cisco-IOS-XE-ospf:process": {
            "id": ospf["process_id"],
            "router-id": ospf["router_id"],
            "network": [
                {
                    "ip": n["network"],
                    "wildcard": n["wildcard"],
                    "area": n["area"]
                } for n in ospf["networks"]
            ]
        }
    }

    restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# ACLs
# ---------------------------------------------------------
def push_acls(device, auth):
    if "acls" not in device:
        return
    acls = device["acls"]

    for acl in acls.get("extended", []):
        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-acl:acl/acl-sets/acl-set={acl['name']},ACL_IPV4"
        )
        entries = []
        seq = 10
        for e in acl["entries"]:
            entry = {
                "sequence-number": seq,
                "ace-type": "ACE_TYPE_EXTENDED",
                "actions": {
                    "forwarding": "PERMIT" if e["action"] == "permit" else "DENY"
                },
                "protocol": e["protocol"].upper(),
                "source-network": {
                    "source-address": e["source"].split()[1] if " " in e["source"] else e["source"]
                },
                "destination-network": {
                    "destination-address": e["destination"].split()[0]
                }
            }
            if "dest_port" in e:
                entry["destination-port"] = {
                    "operator": "EQ",
                    "port": e["dest_port"]
                }
            entries.append(entry)
            seq += 10

        payload = {
            "Cisco-IOS-XE-acl:acl-set": {
                "name": acl["name"],
                "type": "ACL_IPV4",
                "aces": {
                    "ace": entries
                }
            }
        }

        restconf_request("PUT", url, auth, payload)

    if "nat_acl" in acls:
        nat = acls["nat_acl"]
        url = (
            f"https://{device['host']}/restconf/data/"
            f"Cisco-IOS-XE-acl:acl/acl-sets/acl-set={nat['number']},ACL_IPV4"
        )
        entries = []
        seq = 10
        for e in nat["entries"]:
            entry = {
                "sequence-number": seq,
                "ace-type": "ACE_TYPE_STANDARD",
                "actions": {
                    "forwarding": "PERMIT"
                },
                "source-network": {
                    "source-address": e["source"].split()[0]
                }
            }
            entries.append(entry)
            seq += 10

        payload = {
            "Cisco-IOS-XE-acl:acl-set": {
                "name": str(nat["number"]),
                "type": "ACL_IPV4",
                "aces": {
                    "ace": entries
                }
            }
        }

        restconf_request("PUT", url, auth, payload)


# ---------------------------------------------------------
# NAT
# ---------------------------------------------------------
def push_nat(device, auth):
    if "nat" not in device:
        return

    nat = device["nat"]
    pool = nat["pool"]

    url_pool = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-nat:nat/pool={pool['name']}"
    )

    payload_pool = {
        "Cisco-IOS-XE-nat:pool": {
            "name": pool["name"],
            "start-address": pool["start_ip"],
            "end-address": pool["end_ip"],
            "netmask": pool["netmask"]
        }
    }

    restconf_request("PUT", url_pool, auth, payload_pool)

    inside = nat["inside_source"]
    url_inside = (
        f"https://{device['host']}/restconf/data/"
        f"Cisco-IOS-XE-nat:nat/inside/source/list={inside['acl']}"
    )

    payload_inside = {
        "Cisco-IOS-XE-nat:list": {
            "id": inside["acl"],
            "pool": inside["pool"],
            "overload": inside.get("overload", False)
        }
    }

    restconf_request("PUT", url_inside, auth, payload_inside)


# ---------------------------------------------------------
# DHCP SERVICE TOGGLES (very basic)
# ---------------------------------------------------------
def push_dhcp_service(device, auth):
    if "dhcp" not in device:
        return
    dhcp = device["dhcp"]

    url = f"https://{device['host']}/restconf/data/Cisco-IOS-XE-native:native/ip"
    native_ip = {}

    if dhcp.get("service_dhcp") and not dhcp.get("no_service_dhcp"):
        native_ip["dhcp"] = {}
    elif dhcp.get("no_service_dhcp"):
        pass

    if not native_ip:
        return

    payload = {
        "Cisco-IOS-XE-native:ip": native_ip
    }

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
        mgmt = dev.get("management_ip") or dev.get("host")
        if not mgmt:
            print(f"Skipping device {dev.get('name', '<unknown>')} because management_ip is missing.")
            continue

        host = mgmt.split("/")[0]
        dev["host"] = host

        ssh = dev.get("ssh")
        if not ssh or "username" not in ssh or "secret" not in ssh:
            print(f"Skipping device {dev.get('name', '<unknown>')} because SSH credentials are missing.")
            continue

        auth = (ssh["username"], ssh["secret"])
        print(f"\n=== Device {dev.get('name', '<unknown>')} ({dev.get('type', '<unknown>')}) @ {host} ===")

        push_global_native(dev, auth)
        push_vlans(dev, auth)
        push_interfaces(dev, auth)
        push_hsrp(dev, auth)
        push_ospf(dev, auth)
        push_acls(dev, auth)
        push_nat(dev, auth)
        push_dhcp_service(dev, auth)


if __name__ == "__main__":
    main()
