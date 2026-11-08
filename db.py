"""Database layer: schema init + seed curriculum + data access helpers.

Uses only stdlib sqlite3. DB path is configurable via ICS_DB env var.
Seed content is trusted (bundled); lecture HTML is rendered as-is.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DB = str(ROOT / "ics_platform.db")


def db_path() -> str:
    return os.environ.get("ICS_DB", DEFAULT_DB)


def connect(path: str | None = None):
    import sqlite3

    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_password(pw: str, salt_hex: str | None = None) -> str:
    salt = salt_hex or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000)
    return f"pbkdf2_sha256$240000${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, rounds, salt, expected = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def init_db(path: str | None = None):
    conn = connect(path)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    conn.close()
    seed(path)


def seed(path: str | None = None):
    conn = connect(path)
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users (username, email, pw_hash, role) VALUES (?,?,?,?)",
            ("admin", "admin@example.com", hash_password("admin123"), "admin"),
        )
        conn.execute(
            "INSERT INTO users (username, email, pw_hash, role) VALUES (?,?,?,?)",
            ("demo", "demo@example.com", hash_password("demo123"), "user"),
        )
    if conn.execute("SELECT COUNT(*) c FROM courses").fetchone()["c"] == 0:
        cur = conn.execute(
            "INSERT INTO courses (title, description) VALUES (?,?)",
            (
                "ICS/OT Security Fundamentals",
                "Absolute beginners to junior ICS/OT security specialists: "
                "networking, industrial protocols, control architectures and threat analysis.",
            ),
        )
        course_id = cur.lastrowid
        for m in MODULES:
            conn.execute(
                """INSERT OR IGNORE INTO modules
                   (id, course_id, title, description, ord, tmin_s, tmax_s, quiz_tmin_s, quiz_tmax_s, pass_score, lecture_body)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (m["id"], course_id, m["title"], m["description"], m["ord"],
                 m["tmin_s"], m["tmax_s"], m["quiz_tmin_s"], m["quiz_tmax_s"], m["pass_score"], m["lecture_body"]),
            )
            for q in m["quiz"]:
                conn.execute(
                    """INSERT OR IGNORE INTO questions (module_id, ord, q_text, options_json, correct_key, explanation)
                       VALUES (?,?,?,?,?,?)""",
                    (m["id"], q["ord"], q["q_text"], json.dumps(q["options"]), q["correct"], q["explanation"]),
                )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- curriculum
# Tmin/Tmax are intentionally demo-friendly (minutes, not hours). Production
# deployments raise tmin_s via the admin DB, e.g. 300-600s per lecture.
MODULES = [
    {
        "id": "mod-01", "title": "Foundations of Networking for OT",
        "description": "IP, TCP/UDP, switches, VLANs, NAT and network segmentation (Purdue model).",
        "ord": 0, "tmin_s": 90, "tmax_s": 900, "quiz_tmin_s": 30, "quiz_tmax_s": 600, "pass_score": 80,
        "lecture_body": """<h2>Why OT networking is different</h2>
<p>IT networks optimise for speed and flexibility. OT networks optimise for <strong>availability, determinism and safety</strong>. A 200&nbsp;ms hiccup is invisible for web browsing — but can trip a protective relay or upset a chemical process. Almost everything in this course follows from that trade-off.</p>
<h3>Core concepts</h3>
<ul>
<li><strong>IP addressing (IPv4)</strong> — every controller, HMI and historian needs an address. In OT you will meet static IPs, and you must always check for duplicates: two devices sharing an IP is a safety incident, not a nuisance.</li>
<li><strong>TCP vs UDP</strong> — TCP guarantees delivery and order (good for file transfer, SFTP config backups, web HMIs). UDP is connectionless and fast (good for periodic sensor telemetry, DNP3 over UDP, broadcast discovery).</li>
<li><strong>Switches & VLANs</strong> — L2 switches forward by MAC. VLANs split one physical switch into logically isolated networks (e.g. VLAN 10 = control, VLAN 20 = supervision, VLAN 99 = management). VLANs are <em>not</em> a firewall — they do not inspect payloads.</li>
<li><strong>Routers, firewalls & NAT</strong> — firewalls enforce policy between zones; routers interconnect them. Legacy OT devices often cannot run modern auth/TLS, so network controls (allow-lists, jump hosts, unidirectional gateways) compensate.</li>
<li><strong>ICMP & ARP</strong> — ARP maps IP→MAC. ICMP is used by ping and diagnostics. Attackers abuse both (ARP spoofing, ICMP tunnelling), which is why you will monitor them in ICS PCAPs.</li>
</ul>
<h3>The Purdue model (simplified)</h3>
<table><tr><th>Level</th><th>Examples</th><th>Threat focus</th></tr>
<tr><td>0–1 Process</td><td>sensors, actuators, PLCs, RTUs</td><td>physical damage, safety</td></tr>
<tr><td>2 Supervisory</td><td>HMI, local OPC, historians</td><td>operator trust, phishing</td></tr>
<tr><td>3 Operations</td><td>production planning, DMZ</td><td>ransomware, IT→OT pivot</td></tr>
<tr><td>4–5 Enterprise</td><td>ERP, cloud, internet</td><td>commodity malware, remote access VPN</td></tr></table>
<div class="callout"><strong>Defensive habit:</strong> learn to read a packet capture. <code>ping / arp / tcpdump / Wireshark</code> are the OT analyst's daily tools. Always confirm: who is talking, to whom, on which port, at what rate (periodic OT traffic is often highly regular).</div>
<h3>Ports you will meet first</h3>
<p>80/443 (web), 22 (SSH), 53 (DNS), 123 (NTP), 502 (Modbus), 20000 (DNP3), 44818 (EtherNet/IP), 102 (S7), 47808 (BACnet). Memorise 502 and 20000 — they dominate the next module.</p>""",
        "quiz": [
            {"ord": 1, "q_text": "A VLAN separates broadcast domains on one switch. Does it by itself provide firewall-grade security between segments?", "options": {"a": "Yes — VLANs encrypt traffic", "b": "No — VLANs do not inspect payloads; a firewall/router policy is still required", "c": "Yes — only L3 VLANs do, but this is L2", "d": "Only if the switch is managed"}, "correct": "b", "explanation": "VLANs isolate broadcast domains but forward frames without deep inspection. Enforcement requires firewall rules / ACLs between zones."},
            {"ord": 2, "q_text": "Which protocol is connectionless and typically used for high-rate periodic sensor telemetry where loss of a single sample is acceptable?", "options": {"a": "TCP", "b": "TLS over TCP", "c": "UDP", "d": "SMTP"}, "correct": "c", "explanation": "UDP is connectionless/fast. TCP adds reliability and ordering at the cost of latency — often unnecessary for loss-tolerant telemetry."},
            {"ord": 3, "q_text": "Scenario: Two PLCs answer to 192.168.10.5 after a contractor's change. Most likely OT root cause?", "options": {"a": "A slow DNS server", "b": "Duplicate static IP assignment — a safety-relevant addressing error", "c": "A full HMI disk", "d": "An expired TLS certificate"}, "correct": "b", "explanation": "Duplicate IPs cause ARP flapping and missed commands. In OT this is treated as a safety incident: fix addressing and document the scheme."},
            {"ord": 4, "q_text": "In the Purdue model, which level contains HMIs and local historians?", "options": {"a": "Level 0", "b": "Level 1 only", "c": "Level 2 (supervisory)", "d": "Level 5"}, "correct": "c", "explanation": "Level 2 is supervisory: HMI, engineering workstations, local historians. Levels 0-1 are process/control devices."},
            {"ord": 5, "q_text": "An attacker performs ARP spoofing on the control LAN. What can they achieve?", "options": {"a": "Nothing — ARP is encrypted", "b": "Redirect traffic by claiming to be the gateway, enabling interception/man-in-the-middle", "c": "Only hide their MAC address", "d": "Boot a PLC remotely"}, "correct": "b", "explanation": "ARP is unauthenticated. By injecting false IP→MAC mappings the attacker can intercept or relay traffic between victim and gateway."},
            {"ord": 6, "q_text": "Which single network observation is most suspicious in an OT PCAP?", "options": {"a": "A PLC sending the same Modbus read every 500 ms", "b": "A workstation on the control LAN bursting TCP scans across hundreds of PLC IPs/ports", "c": "Regular NTP sync packets", "d": "An HMI polling one PLC"}, "correct": "b", "explanation": "OT polling is periodic and predictable. Broad scans and fan-out across many hosts/ports are classic recon behaviour."},
        ],
    },
    {
        "id": "mod-02", "title": "Industrial Protocols: Modbus & DNP3",
        "description": "How Modbus and DNP3 move values, why they lack authentication, and how to spot abuse.",
        "ord": 1, "tmin_s": 90, "tmax_s": 900, "quiz_tmin_s": 30, "quiz_tmax_s": 600, "pass_score": 80,
        "lecture_body": """<h2>Modbus: the protocol that never dies</h2>
<p>Modbus (1979) defines four data types: coils (1-bit R/W), discrete inputs (1-bit R), holding registers (16-bit R/W), input registers (16-bit R). A <strong>client (formerly master)</strong> polls a <strong>server (formerly slave)</strong> by unit ID over TCP port 502 (Modbus/TCP). There is <strong>no authentication, no encryption, no integrity check</strong> beyond TCP — anyone who can reach port 502 can read and write registers.</p>
<h3>Function codes you must recognise</h3>
<table><tr><th>Code</th><th>Meaning</th><th>Risk</th></tr>
<tr><td>01 / 03</td><td>Read coils / holding registers</td><td>reconnaissance, process disclosure</td></tr>
<tr><td>05 / 06 / 16</td><td>Write single coil / single reg / multiple regs</td><td>direct process manipulation</td></tr>
<tr><td>08 / 43</td><td>Diagnostics / device identification</td><td>fingerprinting</td></tr>
<tr><td>15 / 23</td><td>Write multiple coils / read-write multiple</td><td>large-scale changes</td></tr></table>
<h3>DNP3 (IEEE 1815): built for utility SCADA</h3>
<p>DNP3 adds framing, application sequence numbers, and a rich function-code / object model (Group/Var). Common groups: G1 binary inputs, G2 analog inputs, G12 analog outputs (controls), G51/G52 counters. Confirm (CON) bits, unsolicited responses, and time-sync objects make it more capable than Modbus — but classic DNP3 is equally unauthenticated. <strong>Secure DNP3 (IEEE 1815.2 / SAv5)</strong> adds challenge-response authentication (g120) for critical functions.</p>
<h3>How attacks look on the wire</h3>
<ul>
<li><strong>Recon:</strong> reads across full register ranges, unit-ID sweeps, many rare function codes.</li>
<li><strong>Replay / injection:</strong> a write with plausible values at an implausible time, breaking the periodic pattern.</li>
<li><strong>Flooding / malformed frames:</strong> invalid lengths/function codes targeting parser bugs in embedded stacks.</li>
<li><strong>DNP3 abuse:</strong> unauthorised DIRECT_OPERATE controls, cold-restart / stop-application commands, or IIN bits indicating tampering.</li>
</ul>
<div class="callout"><strong>Defensive habits:</strong> allow-list function codes by zone (most HMIs only need reads), deep-packet-inspect Modbus/DNP3 at Zone 1↔2 conduits, basline the normal register map and polling rate, and alert on any write from a non-HMI source.</div>""",
        "quiz": [
            {"ord": 1, "q_text": "Which Modbus function codes are WRITES?", "options": {"a": "01, 03", "b": "05, 06, 16", "c": "08 only", "d": "None — Modbus is read-only"}, "correct": "b", "explanation": "05=write single coil, 06=write single register, 16=write multiple registers. 01/03 are reads."},
            {"ord": 2, "q_text": "Why is Modbus/TCP inherently unsafe on an untrusted network?", "options": {"a": "It is too slow", "b": "It has no authentication, encryption, or integrity protection per message", "c": "It requires TLS certificates", "d": "It only works over UDP"}, "correct": "b", "explanation": "Classic Modbus/TCP has no auth/crypto. Any device reaching TCP/502 can issue reads and writes."},
            {"ord": 3, "q_text": "Scenario: You see Modbus writes to holding registers from a laptop at 02:00, while the HMI only ever sent reads. Best interpretation?", "options": {"a": "Normal polling", "b": "Suspected unauthorised control / compromise — investigate immediately", "c": "Time-sync traffic", "d": "A firmware update in progress"}, "correct": "b", "explanation": "Writes from a non-operator source, off-hours, breaking the baseline = investigate as potential manipulation."},
            {"ord": 4, "q_text": "In DNP3, DIRECT_OPERATE (FC 0x06) is dangerous because it…", "options": {"a": "Sends data without a MAC header", "b": "Executes a control without the multi-step confirm/select-before-operate discipline", "c": "Encrypts the control value", "d": "Only works on LAN broadcast"}, "correct": "b", "explanation": "DIRECT_OPERATE skips select-before-operate confirmation. An injected control executes immediately. Secure DNP3 mitigates this for critical functions."},
            {"ord": 5, "q_text": "Which DNP3 concept lets devices announce new data without being polled?", "options": {"a": "Unsolicited responses", "b": "TCP keep-alive", "c": "ARP", "d": "DNS"}, "correct": "a", "explanation": "Unsolicited responses let outstations push event data. They are legitimate but must be monitored — flooding them is a DoS vector."},
            {"ord": 6, "q_text": "Best compensating control if a legacy Modbus PLC cannot be upgraded?", "options": {"a": "Move it to the internet with port-forwarding", "b": "Zone/conduit firewall with Modbus DPI allow-listing only required functions and sources", "c": "Disable the firewall to reduce latency", "d": "Rely on VLANs alone"}, "correct": "b", "explanation": "You cannot add auth to classic Modbus. Network-layer allow-listing (source + function code via DPI) is the standard compensation."},
        ],
    },
    {
        "id": "mod-03", "title": "PLC, SCADA & Controller Architectures",
        "description": "Control levels, scan cycles, HMIs, historians, engineering workstations and their trust relationships.",
        "ord": 2, "tmin_s": 120, "tmax_s": 1200, "quiz_tmin_s": 30, "quiz_tmax_s": 600, "pass_score": 80,
        "lecture_body": """<h2>How a plant is actually controlled</h2>
<p><strong>Level 0 — process:</strong> sensors (level, pressure, temperature transmitters) produce 4–20&nbsp;mA / fieldbus signals; actuators (valves, motors, breakers) execute commands. <strong>Level 1 — control:</strong> PLCs/RTUs run <em>scan cycles</em>: read inputs → execute ladder logic (IEC 61131-3) → write outputs, typically every 10–100&nbsp;ms, deterministically. <strong>Safety Instrumented Systems (SIS)</strong> are separate and independent from the basic control system — never assume one safety path protects against a logic change in the other.</p>
<h3>Levels 2–3: supervision and operations</h3>
<ul>
<li><strong>HMI:</strong> operator graphics, alarms, set-point changes. The HMI is a <em>trusted</em> Modbus/DNP3 client — compromising it is equivalent to holding the operator's hand.</li>
<li><strong>Historian:</strong> time-series archive for trending, forensics and regulatory records (OPC UA / OPC DA often bridge Level 1→2).</li>
<li><strong>Engineering Workstation (EWS):</strong> holds thePLC project, ladder logic and firmware tools. EWS compromise = logic compromise. It must be hardened, application allow-listed and rarely network-accessible.</li>
<li><strong>SCADA master:</strong> coordinates RTUs across sites, issues select-before-operate controls, displays system-wide state.</li>
</ul>
<h3>Architecture patterns & failure modes</h3>
<table><tr><th>Pattern</th><th>Strength</th><th>Critical weakness to defend</th></tr>
<tr><td>Direct PLC→HMI LAN</td><td>simple, low latency</td><td>flat network — any L2 foothold reaches controllers</td></tr>
<tr><td>Zoned (Purdue) with conduits</td><td>policy between levels</td><td>misconfigured firewall = false sense of safety; audit rules</td></tr>
<tr><td>Redundant PLC pairs (hot standby)</td><td>availability</td><td>both brains share the same project — protect the single source of truth</td></tr>
<tr><td>Hard-wired interlocks + SIS</td><td>independent protection layer</td><td>must be tested, not just installed</td></tr></table>
<h3>Key mental model</h3>
<p>Think in <strong>trust chains</strong>: Internet → VPN/jump host → operations DMZ → supervisory → control. Each hop should authenticate, allow-list and log. The SCADA operator's legitimate ability to change a set-point must never imply that an internet user has the same capability.</p>
<div class="callout"><strong>Junior operator checks:</strong> (1) Is this device in the asset inventory? (2) Is its traffic inside the documented conduit? (3) Does its logic match the golden backup? Learn to answer all three from a baseline.</div>""",
        "quiz": [
            {"ord": 1, "q_text": "What is the PLC scan cycle?", "options": {"a": "Read inputs → execute logic → write outputs, repeatedly and deterministically", "b": "Backup, archive, then reboot", "c": "Only run user programs on demand", "d": "Synchronise with the historian"}, "correct": "a", "explanation": "The PLC cyclically reads inputs, evaluates ladder logic, and writes outputs on a predictable period (ms scale)."},
            {"ord": 2, "q_text": "Why must the SIS be independent from the basic control system?", "options": {"a": "For performance", "b": "So a logic error/compromise in the BPCS does not also defeat the safety layer", "c": "To reduce wiring", "d": "It is not required to be independent"}, "correct": "b", "explanation": "Independence prevents a single failure from defeating both control and protection layers (IEC 61511 principle)."},
            {"ord": 3, "q_text": "Which asset is the highest-risk target for silent process manipulation?", "options": {"a": "The historian reporting server", "b": "The engineering workstation holding PLC logic", "c": "A CCTV camera", "d": "The corporate email server"}, "correct": "b", "explanation": "The EWS can download logic, change timers/set-points and reprogram interlocks while keeping operator views plausible."},
            {"ord": 4, "q_text": "Scenario: HMI set-point changes are logged but unauthenticated, and the HMI accepts trip overrides. Top finding?", "options": {"a": "No issue — HMIs are trusted", "b": "Missing authentication + excessive privilege at Level 2; require auth, least-privilege, and alarm on overrides", "c": "HMI storage too small", "d": "The historian is offline"}, "correct": "b", "explanation": "Unauthenticated control + override authority violates least-privilege. Require per-operator auth and alert on safety overrides."},
            {"ord": 5, "q_text": "Redundant PLC pairs mainly protect availability. What single point remains?", "options": {"a": "The power supply only", "b": "The shared logic/project source of truth (EWS + backups) — protect it as one asset", "c": "The HMI desk", "d": "Nothing — fully redundant"}, "correct": "b", "explanation": "Both controllers run the same project. Compromise of the project source defeats both."},
            {"ord": 6, "q_text": "The SCADA master issues select-before-operate controls to RTUs. The main security benefit is…", "options": {"a": "Encryption", "b": "A disciplined two-step control path that can be authenticated, rate-limited and logged", "c": "Faster polling", "d": "No WAN needed"}, "correct": "b", "explanation": "SBO separates selection from execution, giving an enforcement and audit point. It does not add crypto by itself — pair it with authentication and conduit controls."},
        ],
    },
    {
        "id": "mod-04", "title": "ICS Threat Vectors & Defensive Fundamentals",
        "description": "Threat models, common attack chains, USB/remote-access risk, and the monitoring & response habits of a junior analyst.",
        "ord": 3, "tmin_s": 120, "tmax_s": 1200, "quiz_tmin_s": 30, "quiz_tmax_s": 600, "pass_score": 80,
        "lecture_body": """<h2>Who attacks ICS and why</h2>
<p>Attackers range from commodity ransomware affiliates and hacktivists to nation-state APTs. Unlike IT, a successful ICS attack can <strong>stop production, damage equipment, or endanger people</strong>. The landmark cases every junior analyst should know by name: <strong>TRISIS/TRITON</strong> (targeted SIS controllers), <strong>Ukraine 2015/2016 power outages</strong> (IT breach → operator console takeover → breaker manipulation), and <strong>Oldsmar 2021</strong> (remote access manipulation of a water-treatment chemical set-point). Different vectors, same lesson: the IT→OT bridge and remote access are the usual entry.</p>
<h3>Common chains (MITRE ATT&CK for ICS)</h3>
<ol>
<li><strong>Initial access:</strong> phishing, exposed VPN/RDP, vendor remote access, removable media, internet-facing HMI.</li>
<li><strong>Discovery/recon:</strong> network scans, passive asset enumeration, register-map fingerprinting.</li>
<li><strong>Persistence/privilege:</strong> new accounts on HMI/EWS, modified logic, rogue OPC server.</li>
<li><strong>Impact:</strong> inhibit response (block alarms), impair process control (logic/set-point change), destroy/replace data.</li>
</ol>
<h3>Defensive fundamentals (what your first job will actually ask)</h3>
<ul>
<li><strong>Segmentation & conduits:</strong> Purdue zones, deny-by-default firewalls, jump hosts with MFA for any remote access. No direct internet→PLC paths, ever.</li>
<li><strong>Allow-list & harden:</strong> application allow-listing on EWS/HMI, disable USB/USB-storage on control hosts, unique accounts, patch during outages (compensate with network controls in between).</li>
<li><strong>Monitor:</strong> passive ICS-aware IDS (e.g. Snort/Suricata with ICS rules, Claroty/Nozomi style baselines), syslog/CEF to SIEM, alert on: new Modbus master, write function codes, logic transfers, SIS mode changes.</li>
<li><strong>Backups & recovery:</strong> offline, tested PLC project & HMI image backups; recovery drill before you need it. Keep golden configs under version control.</li>
<li><strong>Incident response:</strong> preserve PCAP + historian + logic diff, keep the process safe first (safe-state), do not plug unknown USBs into evidence machines, coordinate with operations before rebooting controllers.</li>
</ul>
<div class="callout"><strong>Your first triage questions:</strong> Is the process safe? What changed (logic, set-point, network path)? Who/what sent the last write? Is the SIS still in RUN? Answer in that order.</div>
<h3>Threat-model exercise</h3>
<p>Draw the Purdue diagram for a small water plant. Mark every remote-access path, every USB insertion point, and every device that can issue a Modbus write. If an untrusted path reaches a write-capable device, you have found a finding. That is junior-analyst work — and this course's exit skill.</p>""",
        "quiz": [
            {"ord": 1, "q_text": "Which landmark case targeted Safety Instrumented System controllers directly?", "options": {"a": "Oldsmar 2021", "b": "TRISIS/TRITON", "c": "Ukraine 2015", "d": "SolarWinds IT only"}, "correct": "b", "explanation": "TRITON/TRISIS malware targeted Schneider Triconex SIS controllers — the attacker sought to defeat the safety layer itself."},
            {"ord": 2, "q_text": "In ATT&CK for ICS, 'inhibit response' means the attacker…", "options": {"a": "Speeds up the HMI", "b": "Blocks/suppresses alarms and operator visibility so manipulation goes unnoticed", "c": "Installs antivirus", "d": "Backs up logic"}, "correct": "b", "explanation": "Inhibit Response Function includes blocking reporting and alarm suppression — blinding the operator before/while impairing control."},
            {"ord": 3, "q_text": "Scenario: A vendor needs remote access to the HMI. Safest design?", "options": {"a": "Direct TeamViewer on the HMI, always on", "b": "MFA jump-host in the operations DMZ, time-boxed, session-recorded, deny-by-default", "c": "Open port 3389 to the internet", "d": "Shared vendor account, no expiry"}, "correct": "b", "explanation": "Jump host + MFA + recording + time-boxing minimises standing access and gives an audit trail."},
            {"ord": 4, "q_text": "Removable media is a classic ICS vector primarily because…", "options": {"a": "USB is faster than Ethernet", "b": "Control hosts are often air-gapped, so USB is the bridge for malware and logic/project transfer", "c": "USB cannot carry malware", "d": "Only printers use USB"}, "correct": "b", "explanation": "Air-gaps make USB the practical bridge. Scan media at a kiosk, block storage on control hosts, and log insertions."},
            {"ord": 5, "q_text": "First four triage questions after a suspected ICS manipulation, in priority order?", "options": {"a": "Patch, reboot, reimage, report", "b": "Process safe? What changed? Who sent the last write? Is SIS in RUN?", "c": "Call the vendor, pay, restore, resume", "d": "Wipe the historian first"}, "correct": "b", "explanation": "Safety first, then change/actor assessment, then SIS state — before any reboot or remediation that could worsen the process."},
            {"ord": 6, "q_text": "Which monitoring alert is highest priority for a junior analyst?", "options": {"a": "Routine NTP sync", "b": "New Modbus master issuing write function codes to a PLC", "c": "Historian archive rotation", "d": "HMI screensaver"}, "correct": "b", "explanation": "A new write-capable source breaks the baseline trust chain. Routine periodic polls and NTP are expected background."},
        ],
    },
]


# ---------------------------------------------------------------- progress helpers
def ensure_progress_row(conn, user_id: int, module_id: str):
    conn.execute(
        "INSERT OR IGNORE INTO progress (user_id, module_id) VALUES (?,?)", (user_id, module_id)
    )


def get_module(conn, module_id: str):
    row = conn.execute("SELECT * FROM modules WHERE id=?", (module_id,)).fetchone()
    return dict(row) if row else None


def list_modules(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM modules ORDER BY ord").fetchall()]


def module_by_ord(conn, ord_: int):
    row = conn.execute("SELECT * FROM modules WHERE ord=?", (ord_,)).fetchone()
    return dict(row) if row else None


def is_unlocked(conn, user_id: int, module: dict) -> bool:
    own = conn.execute(
        "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user_id, module["id"])
    ).fetchone()
    if own and own["quiz_passed"]:
        return True
    if module["ord"] == 0:
        return True
    if own:
        return True  # unlocked earlier (row exists) even if not yet completed
    prev = module_by_ord(conn, module["ord"] - 1)
    if not prev:
        return False
    prow = conn.execute(
        "SELECT * FROM progress WHERE user_id=? AND module_id=?", (user_id, prev["id"])
    ).fetchone()
    return bool(prow and prow["lecture_verified"] and prow["quiz_passed"])


def unlock_grant(conn, user_id: int, module_id: str):
    ensure_progress_row(conn, user_id, module_id)


def completed_count(conn, user_id: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) c FROM progress p JOIN modules m ON m.id=p.module_id
           WHERE p.user_id=? AND p.lecture_verified=1 AND p.quiz_passed=1""",
        (user_id,),
    ).fetchone()["c"]
