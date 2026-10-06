# GMIA audit report page

`build_audit_page.py` generates `hedge-fund-research-audit.html`, the static
stage-1/stage-2 audit report published on docs.sinostor.com.cn. The page's
content lives in this script (the finding tables, the narrative, the commit
links), so this is the file to edit when the report changes.

    python3 docs/audit-report/build_audit_page.py /tmp/out.html
    cp /tmp/out.html ~/docs-site/pages/hedge-fund-research-audit.html
    cd ~/docs-site && bash scripts/verify-pages.sh && \
      PAGE_LIST="hedge-fund-research-audit.html" bash scripts/publish.sh

Before publishing: every `/commit/<sha>` link must exist on origin/main and
sit beside the claim it supports (the 2026-10-02 build caught one that did
not), and quoted constants must match the code. The page is registered in
docs-site (page-manifest tier C, CSS_EXEMPT) and in infra-scripts' PAGE_MAP.
