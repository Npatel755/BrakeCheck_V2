
"""Create and display a short-lived BrakeCheck pairing QR code."""

import argparse
import json
import os
import tempfile
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit
from getpass import getpass

import qrcode
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def normalize_backend_url(raw: str) -> str:
    """Accept HTTPS cloud URLs and private HTTP URLs used by LAN demos."""
    parts = urlsplit(raw.strip().rstrip("/"))
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise ValueError("Use an HTTPS base URL such as https://your-service.up.railway.app.")

    if parts.scheme == "http":
        host = parts.hostname.lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        local_name = host == "localhost" or host.endswith(".localhost")
        if not local_name and (address is None or not address.is_private or address.is_loopback):
            raise ValueError("Public cloud backends must use HTTPS. HTTP is allowed only on a private LAN.")

    return f"{parts.scheme}://{parts.netloc}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a five-minute BrakeCheck phone-pairing QR code.")
    parser.add_argument(
        "--backend-url",
        default=os.getenv("BRAKECHECK_PUBLIC_URL", "").strip(),
        help="HTTPS URL for a hosted backend; omit for private-Wi-Fi pairing.",
    )
    args = parser.parse_args(argv)

    is_cloud = bool(args.backend_url)
    if is_cloud:
        try:
            backend_url = normalize_backend_url(args.backend_url)
        except ValueError as exc:
            raise SystemExit(str(exc))
        # Prompt rather than placing the cloud admin key in command history.
        key = getpass("Cloud BRAKECHECK_APP_KEY: ").strip()
        issue_url = f"{backend_url}/pairing/issue"
    else:
        key = os.getenv("BRAKECHECK_APP_KEY", "").strip()
        port = int(os.getenv("BRAKECHECK_PORT", "8000"))

    if len(key) < 24:
        if is_cloud:
            raise SystemExit("The cloud app key must contain at least 24 characters.")
        raise SystemExit("BRAKECHECK_APP_KEY is missing. Run configure.py first.")

    if not is_cloud:
        # Ask for the PC's local IPv4 address.
        host = input("Enter your PC's Wi-Fi IPv4 address: ").strip()
        try:
            address = ipaddress.ip_address(host)
            if address.version != 4 or not address.is_private or address.is_loopback:
                raise ValueError
        except ValueError:
            raise SystemExit("Enter a valid private IPv4 address, not localhost.")
        backend_url = f"http://{host}:{port}"
        issue_url = f"http://127.0.0.1:{port}/pairing/issue"

    try:
        response = requests.post(
            issue_url,
            headers={"X-BrakeCheck-Key": key},
            timeout=5,
        )

        response.raise_for_status()
        code = response.json()["code"]

    except requests.RequestException as exc:
        raise SystemExit(
            f"Could not reach BrakeCheck backend: {exc}"
        )

    except (ValueError, KeyError):
        raise SystemExit(
            "The backend returned an unexpected pairing response."
        )

    # Include both the backend URL and temporary code.
    payload = json.dumps(
        {
            "app": "BrakeCheck",
            "version": 1,
            "backend_url": backend_url,
            "code": code,
        },
        separators=(",", ":"),
    )

    image = qrcode.make(payload)

    handle, filename = tempfile.mkstemp(
        prefix="brakecheck-pair-",
        suffix=".png",
    )

    os.close(handle)
    image.save(filename)

    print(f"Backend URL: {backend_url}")
    print("Scan this QR code in BrakeCheck within 5 minutes.")
    print("Keep the QR private; it grants access to your backend.")

    try:
        if os.name == "nt":
            os.startfile(filename)
        else:
            print(f"QR image: {filename}")

        input("Press Enter after pairing finishes. ")

    finally:
        try:
            Path(filename).unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
