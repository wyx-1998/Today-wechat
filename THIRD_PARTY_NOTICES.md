# Third Party Notices

Today-wechat's original code is licensed under MIT. Third-party code and dependencies retain their own licenses; the root license does not replace them.

## Legacy source code

The following compatibility scripts originate from or are based on [huangserva/wechat-assistant](https://github.com/huangserva/wechat-assistant), whose README declares MIT. The attribution reference is commit `770a34fd103247fc68e228ee89dc75af7c4f3c61`; local modifications are included in this project.

- `scripts/collector.py`
- `scripts/refresh_decrypt.py`
- `scripts/decrypt/config.py`
- `scripts/decrypt/decrypt_db.py`
- `config.example.yaml`

Upstream also credits `bbingz/wechat-decrypt` for its earlier decryption work. Its process-memory key extractor is excluded from this public distribution. This project does not claim exclusive authorship of the legacy scripts.

The following MIT notice is retained for the upstream code:

```text
MIT License

Copyright (c) huangserva and upstream contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Dependencies

Dependencies are installed from package registries rather than vendored into this source repository. Their copyright and license files must be retained when redistributing an application that includes them. Exact JavaScript and Rust versions are recorded in `package-lock.json` and `Cargo.lock`.

| Component | License |
| --- | --- |
| React, React DOM, react-markdown | MIT |
| AG-UI client and core | MIT |
| RxJS | Apache-2.0 |
| Tauri and Tauri JavaScript API | MIT OR Apache-2.0 |
| PyYAML | MIT |
| PyCryptodome | BSD-2-Clause and portions in the public domain; see its distributed license |
| python-zstandard | BSD-3-Clause; bundled Zstandard has its own notices |
| PyInstaller build tooling | GPL-2.0-or-later with a bootloader exception; see its distributed license |

This table summarizes principal dependencies and does not replace individual license texts or notices for transitive dependencies. No prebuilt application or installer is included in the initial source publication.
