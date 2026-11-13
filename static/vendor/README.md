# Vendored frontend assets

Pinned release files, copied unmodified from the npm registry tarballs. No build step and no
runtime CDN. To upgrade, download the new tarball, check its `dist.integrity` against the
registry, replace the directory, update this table and
[Third-party notices](../../THIRD_PARTY_NOTICES.md), and update the paths in the templates that
load the files (`templates/layouts/document.html` and `templates/500.html`). Collect static assets
afterwards.

| Package | Version | Files | License | Tarball integrity (sha512) |
| --- | --- | --- | --- | --- |
| bootstrap | 5.3.8 | `dist/css/bootstrap.min.css`, `dist/js/bootstrap.bundle.min.js` (+ maps) | MIT | `HP1SZDqaLDPwsNiqRqi5NcP0SSXciX2s9E+RyqJIIqGo+vJeN5AJVM98CXmW/Wux0nQ5L7jeWUdplCEf0Ee+tg==` |
| htmx.org | 2.0.11 | `dist/htmx.min.js` | 0BSD | `Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` |
| datatables.net | 3.1.5 | `dataTables.min.js`, `License.txt` | MIT | `phu9fVz1cOhD7kZDO5Ztj15qwghCkmdwX5AezLi+PxfMtnFAq0itpewvXzdwdr6bOwPqO2zuXsXWUU5x3wQptw==` |
| datatables.net-bs5 | 3.1.5 | `dataTables.bootstrap5.min.js`, `dataTables.bootstrap5.min.css` | MIT | `eGSe7DVcqqI0RKtrjgzFVqqCZoltJSahAKLCJ0nH0WzSfVclhD8maoGbbz/JJQXky1H9c+vzAnbmAevYRBjxOw==` |
| datatables.net-colreorder | 3.1.2 | `dataTables.colReorder.min.js` | MIT | `+PckBw8ayHeOXk1qEWMZLzEAHXu4nSVGb5ef7PTFqwYQrPLsZ0xa6A9boUu1se+4xQj/4vPtZ+0Hj3HiIunA3A==` |
| datatables.net-colreorder-bs5 | 3.1.2 | `colReorder.bootstrap5.min.css` | MIT | `4uBj32P0wHDsCW8+9Goy8fD7HPDUQNANeJqU+2nTkVESDrgU7I4R7PBHi+W9TG84cBixpUptPg8wtzL4t3I0EQ==` |

`static/img/icons.svg` is a subset of Bootstrap Icons 1.13.2 (MIT); its licence is
`static/img/icons.LICENSE`.

Chart.js and Tom Select are not vendored: no Vapor Phase page draws a chart or enhances a picker
yet. Add them the same way when one does.
