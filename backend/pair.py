
"""Create and display a short-lived BrakeCheck pairing QR code."""

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from getpass import getpass

import qrcode
import requests
from dotenv import load_dotenv
from lan import is_private_ipv4

load_dotenv(Path(__file__).with_name(".env"))


def normalize_backend_url(raw: str) -> str:
    """Accept HTTPS cloud URLs and private HTTP URLs used by LAN demos."""
    parts = urlsplit(raw.strip().rstrip("/"))
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise ValueError("Use an HTTPS base URL such as https://your-service.up.railway.app.")
    # Accessing .port also rejects nonnumeric and out-of-range URL ports.
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Use a backend port between 1 and 65535.")

    if parts.scheme == "http" and not is_private_ipv4(parts.hostname):
        raise ValueError("Public cloud backends must use HTTPS. For local HTTP use the PC's private Wi-Fi IPv4 address.")

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
        try:
            port = int(os.getenv("BRAKECHECK_PORT", "8000"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise SystemExit("BRAKECHECK_PORT must be a number between 1 and 65535.")

    if len(key) < 24:
        if is_cloud:
            raise SystemExit("The cloud app key must contain at least 24 characters.")
        raise SystemExit("BRAKECHECK_APP_KEY is missing. Run configure.py first.")

    if not is_cloud:
        # Ask for the PC's local IPv4 address.
        host = input("Enter your PC's Wi-Fi IPv4 address: ").strip()
        if not is_private_ipv4(host):
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
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("code"), str) or not 32 <= len(result["code"]) <= 128:
            raise ValueError("Invalid pairing code.")
        code = result["code"]

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
