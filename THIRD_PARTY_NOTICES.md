# Third-party notices

hoyo-roster is MIT licensed. The following **direct runtime dependency versions** were checked against their installed distribution metadata and shipped license texts. Copies are retained in `third_party/` and the release wheel's `.dist-info/licenses/`. Dependencies are installed separately; their code is not vendored into this package.

| Dependency | Version | License | Retained text |
|---|---|---|---|
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.25.0 | MIT | third_party/jsonschema-COPYING |
| [referencing](https://github.com/python-jsonschema/referencing) | 0.36.2 | MIT | third_party/referencing-COPYING |
| [rfc3339-validator](https://github.com/naimetti/rfc3339-validator) | 0.1.4 | MIT | third_party/rfc3339-validator-LICENSE |
| [simplejson](https://github.com/simplejson/simplejson) | 3.20.1 | MIT and included component notices | third_party/simplejson-LICENSE.txt |
| [portalocker](https://pypi.org/project/portalocker/3.2.0/) | 3.2.0 | BSD-3-Clause | third_party/portalocker-LICENSE |
| [platformdirs](https://pypi.org/project/platformdirs/4.5.0/) | 4.5.0 | MIT | third_party/platformdirs-LICENSE |
| [pywin32](https://pypi.org/project/pywin32/311/) (Windows only) | 311 | PSF; shipped component notices retained | third_party/pywin32-{win32,pythonwin,win32com,adodbapi}-LICENSE.txt |
| [Playwright Python](https://github.com/microsoft/playwright-python) | 1.54.0 | Apache-2.0 | third_party/playwright-LICENSE |

The added dependencies are pinned to published stable releases rather than unverified latest version numbers. portalocker/platformdirs metadata and licenses were read from the installed exact distributions; pywin32 metadata and all four shipped license files were read from the official `311` CPython 3.12 win_amd64 wheel without executing it on macOS. Its metadata labels the project PSF; the retained win32 text states its upstream BSD-style redistribution terms, and pythonwin/win32com redistribution notices and the adodbapi LGPL-2.1 text are preserved separately. hoyo-roster only imports the Win32 filesystem/security bindings, not adodbapi, pythonwin or win32com, and bundles no dependency code. This is a license check, not Windows runtime acceptance. pywin32 is conditional on `sys_platform == 'win32'`; no global post-install command is needed or run.

The retained upstream texts include their copyright and component-specific terms; the simplejson file also records its decoder's BSD terms. No standalone Playwright NOTICE was present in the installed Python distribution's dist-info; its bundled driver retains its upstream third-party notices in the dependency installation. No Chromium binary or driver is copied into hoyo-roster artifacts. Running `python -m playwright install chromium` obtains browser software separately under its upstream distribution terms.

Transitive dependencies and developer/build tools retain their own installed notices. If dependencies or binaries are ever bundled into a different distribution, inspect that exact distribution and include its required notices before shipping; this direct-dependency list does not authorize or document such bundling.

MiHoYo/HoYoverse services, brands, game names and game assets remain subject to their owners' rights and service terms. This project's MIT license does not grant access to accounts or permission to use official assets, marks or interfaces.
