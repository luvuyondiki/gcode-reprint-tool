#!/usr/bin/env python3
"""Simple web server for the G-code resume tool."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from gcode_resume import (
    ResumeParams,
    _extract_temps,
    analyze_gcode,
    build_path_preview,
    generate_resume_gcode,
)

WEB_DIR = Path(__file__).parent / "web"
PORT = 8765


class ResumeHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/style.css":
            self._send_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            return
        if self.path == "/app.js":
            self._send_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/analyze":
            self._handle_analyze()
            return
        if self.path == "/api/resume":
            self._handle_resume()
            return
        self.send_error(404)

    def _handle_analyze(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(400, {"error": "Expected multipart form data"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            form = self._parse_multipart(body, content_type)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        gcode_bytes = form.get("gcode")
        if not gcode_bytes:
            self._send_json(400, {"error": "No G-code file provided"})
            return

        lines = gcode_bytes.decode("utf-8", errors="replace").splitlines()
        analysis = analyze_gcode(lines)
        hotend_temp, bed_temp = _extract_temps(lines)

        self._send_json(
            200,
            {
                "layers": analysis.layer_z_values,
                "min_z": analysis.min_z,
                "max_z": analysis.max_z,
                "min_x": analysis.min_x,
                "max_x": analysis.max_x,
                "min_y": analysis.min_y,
                "max_y": analysis.max_y,
                "hotend_temp": hotend_temp,
                "bed_temp": bed_temp,
            },
        )

    def _handle_resume(self) -> None:

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(400, {"error": "Expected multipart form data"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            form = self._parse_multipart(body, content_type)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            params = ResumeParams(
                stopped_z=float(form["stopped_z"]),
                current_x=float(form["current_x"]),
                current_y=float(form["current_y"]),
                current_z=float(form["current_z"]),
                z_lift=float(form.get("z_lift", "2")),
            )
            require_extrusion = form.get("require_extrusion", "true").lower() != "false"
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": f"Invalid parameters: {exc}"})
            return

        gcode_bytes = form.get("gcode")
        if not gcode_bytes:
            self._send_json(400, {"error": "No G-code file provided"})
            return

        lines = gcode_bytes.decode("utf-8", errors="replace").splitlines()
        result = generate_resume_gcode(lines, params, require_extrusion=require_extrusion)
        output = "\n".join(result.output_lines) + "\n"
        preview = build_path_preview(lines, params, result)
        join_warnings = preview["join_info"].get("warnings", [])
        all_warnings = list(dict.fromkeys(result.warnings + join_warnings))

        self._send_json(
            200,
            {
                "gcode": output,
                "resume_line": result.resume_line_index + 1,
                "resume_x": result.resume_x,
                "resume_y": result.resume_y,
                "resume_z": result.resume_z,
                "lines_removed": result.lines_removed,
                "hotend_temp": result.hotend_temp,
                "bed_temp": result.bed_temp,
                "warnings": all_warnings,
                "warning": "; ".join(all_warnings) if all_warnings else None,
                "layers": result.analysis.layer_z_values if result.analysis else [],
                "max_z": result.analysis.max_z if result.analysis else None,
                "preview": preview,
            },
        )

    def _parse_multipart(self, body: bytes, content_type: str) -> dict[str, str | bytes]:
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part.split("=", 1)[1].strip().strip('"')
                break
        if not boundary:
            raise ValueError("Missing multipart boundary")

        delimiter = ("--" + boundary).encode("utf-8")
        sections = body.split(delimiter)
        form: dict[str, str | bytes] = {}

        for section in sections[1:]:
            if section in (b"--\r\n", b"--", b"\r\n"):
                continue
            if section.startswith(b"\r\n"):
                section = section[2:]
            if section.endswith(b"\r\n"):
                section = section[:-2]

            header_end = section.find(b"\r\n\r\n")
            if header_end == -1:
                continue

            headers = section[:header_end].decode("utf-8", errors="replace")
            data = section[header_end + 4 :]
            if data.endswith(b"\r\n"):
                data = data[:-2]

            name = None
            for line in headers.split("\r\n"):
                if "name=" in line:
                    name = line.split('name="')[1].split('"')[0]
                    break
            if name:
                form[name] = data

        return form

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.address_string()}] {format % args}")


def main() -> None:
    server = HTTPServer(("127.0.0.1", PORT), ResumeHandler)
    print(f"G-code Resume Tool running at http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
