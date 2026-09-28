# Third party licenses

Everything third party is vendored in this repo. Nothing is loaded from a CDN.

| Files | Project | Upstream | License |
|---|---|---|---|
| `app/static/vendor/aruco.js`, `app/static/vendor/cv.js` | js-aruco2 | https://github.com/damianofalcioni/js-aruco2 | MIT, see `JS_ARUCO2_LICENSE` |

The vendored files are unmodified copies. Each one carries the MIT text and
its copyright lines in its header:

- `aruco.js`: Copyright (c) 2020 Damiano Falcioni, Copyright (c) 2011 Juan Mellado
- `cv.js`: Copyright (c) 2011 Juan Mellado

js-aruco2 reads the ArUco tags in the live view and generates the tag images
on the label sheet.
