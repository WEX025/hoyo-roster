# Third-party notices

hoyo-roster is MIT licensed. The following **direct runtime dependency versions** were checked against their installed distribution metadata and shipped license texts. Copies are retained in `third_party/` and the release wheel's `.dist-info/licenses/`. Dependencies are installed separately; their code is not vendored into this package.

| Dependency | Version | License | Retained text |
|---|---|---|---|
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.25.0 | MIT | third_party/jsonschema-COPYING |
| [referencing](https://github.com/python-jsonschema/referencing) | 0.36.2 | MIT | third_party/referencing-COPYING |
| [rfc3339-validator](https://github.com/naimetti/rfc3339-validator) | 0.1.4 | MIT | third_party/rfc3339-validator-LICENSE |
| [simplejson](https://github.com/simplejson/simplejson) | 3.20.1 | MIT and included component notices | third_party/simplejson-LICENSE.txt |
| [Playwright Python](https://github.com/microsoft/playwright-python) | 1.54.0 | Apache-2.0 | third_party/playwright-LICENSE |

The retained upstream texts include their copyright and component-specific terms; the simplejson file also records its decoder's BSD terms. No standalone Playwright NOTICE was present in the installed Python distribution's dist-info; its bundled driver retains its upstream third-party notices in the dependency installation. No Chromium binary or driver is copied into hoyo-roster artifacts. Running `python -m playwright install chromium` obtains browser software separately under its upstream distribution terms.

Transitive dependencies and developer/build tools retain their own installed notices. If dependencies or binaries are ever bundled into a different distribution, inspect that exact distribution and include its required notices before shipping; this direct-dependency list does not authorize or document such bundling.

MiHoYo/HoYoverse services, brands, game names and game assets remain subject to their owners' rights and service terms. This project's MIT license does not grant access to accounts or permission to use official assets, marks or interfaces.
