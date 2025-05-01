import os
import re
import requests
import sys

def is_allowed_target_branch(base):
    return base in ["release", "sandbox", "master", "test-release"]

def infer_jira_fixversion():
    sources = {
        "JIRA_FIXVERSION": os.getenv("JIRA_FIXVERSION"),
        "PR_TITLE": os.getenv("PR_TITLE"),
        "PR_BASE": os.getenv("PR_BASE"),
    }
    for name, val in sources.items():
        if val:
            match = re.search(r"\bBE[- ]\d+\.\d+\.\d+\b", val, re.IGNORECASE)
            if match:
                return match.group(0).replace("-", " ").upper()

    print("❌ Could not infer Jira fixVersion from environment.")
    for k, v in sources.items():
        print(f"{k}: {v}")
    sys.exit(1)

def get_github_commit_task_ids(repo, pr_number, token):
    all_commits = []
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits?per_page=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    while url:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        all_commits.extend(response.json())
        url = None
        if "Link" in response.headers:
            for link in response.headers["Link"].split(", "):
                if 'rel="next"' in link:
                    url = link[link.index("<")+1:link.index(">")]

    messages = [c["commit"]["message"] for c in all_commits]
    task_ids = set()
    for msg in messages:
        task_ids.update(re.findall(r"[A-Z]{2,10}-\d+", msg))
    return sorted(task_ids)

def get_jira_task_ids(domain, user, token, project, version):
    auth = (user, token)
    jql = f'project="{project}" AND fixVersion="{version}"'
    url = f"https://{domain}/rest/api/3/search?jql={jql}&fields=key&maxResults=1000"
    headers = {"Accept": "application/json"}

    response = requests.get(url, headers=headers, auth=auth)
    response.raise_for_status()
    data = response.json()
    return sorted(issue["key"] for issue in data["issues"])

def post_pr_comment(repo, pr_number, token, body):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    response = requests.post(url, headers=headers, json={"body": body})
    response.raise_for_status()

def main():
    repo = os.getenv("GITHUB_REPO")
    pr_number = os.getenv("GITHUB_PR")
    pr_head = os.getenv("PR_HEAD")
    pr_base = os.getenv("PR_BASE")
    github_token = os.getenv("GITHUB_TOKEN")

    jira_user = os.getenv("CI_JIRA_USER")
    jira_token = os.getenv("CI_JIRA_API_TOKEN")
    jira_domain = os.getenv("CI_JIRA_DOMAIN")
    jira_project = os.getenv("JIRA_PROJECT")
    jira_version = infer_jira_fixversion()

    if not is_allowed_target_branch(pr_base):
        print(f"ℹ️ Skipping validation: PR targets `{pr_base}`, which is not a tracked release branch.")
        return

    github_ids = get_github_commit_task_ids(repo, pr_number, github_token)
    jira_ids = get_jira_task_ids(jira_domain, jira_user, jira_token, jira_project, jira_version)

    missing = sorted(set(github_ids) - set(jira_ids))

    comment = f"""### 🔍 Jira/GitHub Task Validation
**PR:** #{pr_number}  
**From:** `{pr_head}` → `{pr_base}`  
**FixVersion:** `{jira_version}`  
**Jira Project:** `{jira_project}`

---

**🧾 Tasks in commits:** `{', '.join(github_ids) or 'None'}`  
**📦 Tasks in Jira release:** `{', '.join(jira_ids) or 'None'}`  
"""

    exit_code = 0

    if not missing:
        comment += "\n✅ All commit tasks are included in the Jira FixVersion. Great job!"
    else:
        comment += f"\n❌ The following tasks appear in commits but are *not* in Jira FixVersion `{jira_version}`:\n- " + "\n- ".join(missing)
        exit_code = 1

    post_pr_comment(repo, pr_number, github_token, comment)
    if exit_code:
        print("❌ Jira validation FAILED. See PR comment for details.")
    else:
        print("✅ Jira validation PASSED.")

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
