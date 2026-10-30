#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write an SPDX 2.3 JSON SBOM for the GSExplorer+ .deb from licenses.json.

Usage: make_sbom.py LICENSES_JSON VERSION > gsexplorer-plus_VERSION.spdx.json
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import uuid


def spdx_id(name: str) -> str:
    return "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-")


def license_expr(s: str) -> str:
    s = (s or "").strip()
    if not s or s == "UNKNOWN":
        return "NOASSERTION"
    if s.startswith("LicenseRef-") or re.fullmatch(r"[A-Za-z0-9.+\- ()]+(WITH [A-Za-z0-9.-]+)?", s.replace(" OR ", " ").replace(" AND ", " ")):
        return s.replace("WITH Bootloader-exception", "WITH LicenseRef-PyInstaller-Bootloader-exception")
    return "NOASSERTION"


def main() -> int:
    inv = json.load(open(sys.argv[1], encoding="utf-8"))
    version = sys.argv[2]
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    app = {
        "SPDXID": "SPDXRef-GSExplorerPlus", "name": "gsexplorer-plus", "versionInfo": version,
        "supplier": "Person: Gus Salazar", "downloadLocation": "https://github.com/gstvslzr/GSExplorer",
        "homepage": "https://github.com/gstvslzr/GSExplorer", "licenseConcluded": "GPL-3.0-or-later",
        "licenseDeclared": "GPL-3.0-or-later", "copyrightText": "2026 Gus Salazar", "filesAnalyzed": False,
        "primaryPackagePurpose": "APPLICATION",
    }
    pkgs, rels = [app], [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                          "relatedSpdxElement": app["SPDXID"]}]
    for c in inv.get("components", []):
        sid = spdx_id(c["name"])
        lic = license_expr(c.get("spdx", ""))
        pkgs.append({"SPDXID": sid, "name": c["name"], "versionInfo": c.get("version") or "NOASSERTION",
                     "downloadLocation": c.get("homepage") or "NOASSERTION", "licenseConcluded": lic,
                     "licenseDeclared": lic, "copyrightText": "NOASSERTION", "filesAnalyzed": False,
                     "comment": c.get("notes", "")})
        rels.append({"spdxElementId": app["SPDXID"], "relationshipType": "CONTAINS", "relatedSpdxElement": sid})
    doc = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"gsexplorer-plus-{version}",
        "documentNamespace": f"https://github.com/gstvslzr/GSExplorer/spdx/{version}-{uuid.uuid4()}",
        "creationInfo": {"created": now, "creators": ["Tool: GSExplorer-make_sbom.py", "Person: Gus Salazar"]},
        "packages": pkgs, "relationships": rels,
        "hasExtractedLicensingInfos": [{
            "licenseId": "LicenseRef-PyInstaller-Bootloader-exception",
            "extractedText": "GPL-2.0-or-later with an exception allowing the bootloader to be used to build "
                             "and distribute programs under any license (see PyInstaller COPYING.txt).",
            "name": "PyInstaller bootloader exception"}],
    }
    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
