import argparse
from collections import defaultdict
from pathlib import Path
import os
import sys

from conda_smithy.linter import conda_recipe_v1_linter
from conda_smithy.linter.utils import (
    get_section,
)
from conda_smithy.utils import get_yaml, render_meta_yaml
import github
import requests


def _lint_recipes(gh, pr):
    lints = defaultdict(list)
    hints = defaultdict(list)
    fnames = set(f.filename for f in pr.get_files())
    labels = set(label.name for label in pr.get_labels())

    # 1. Do not edit or delete example recipes
    for fname in fnames:
        if fname in ["recipes/example/meta.yaml", "recipes/example-v1/recipe.yaml"]:
            lints[fname].append("Do not edit or delete example recipes in `recipes/example/` or `recipe/example-v1/`.")

    # 2. Make sure the new recipe is in the right directory
    for fname in fnames:
        if (
            fname.startswith("recipes/example/")
            or fname.startswith("recipes/example-v1/")
            or fname.startswith("recipes/meta.y")
            or fname.startswith("recipes/recipe.y")
        ):
            lints[fname].append(
                "Please put your recipe in its own directory in the `recipes/` directory as "
                "`recipe/<name of feedstock>/<your recipe file>.yaml`."
            )

    # 3. Only edit recipe files
    if "maintenance" not in labels:
        for fname in fnames:
            if not fname.startswith("recipes/"):
                lints[fname].append("Do not edit files outside of the `recipes/` directory.")

    # Recipe-specific lints/hints
    for fname in fnames:
        if (
            (not fname.startswith("recipes/"))
            or fname == "recipes/example/meta.yaml"
            or fname == "recipes/example-v1/recipe.yaml"
        ):
            continue

        # grab basic metadata
        if fname.endswith("meta.yaml"):
            with open(fname) as fh:
                content = render_meta_yaml("".join(fh))
                meta = get_yaml().load(content)
            recipe_version = 0
        else:
            meta = get_yaml().load(Path(fname))
            recipe_version = 1

        package_section = get_section(
            meta, "package", lints, recipe_version=recipe_version
        )
        sources_section = get_section(
            meta, "source", lints, recipe_version=recipe_version
        )
        extra_section = get_section(
            meta, "extra", lints, recipe_version=recipe_version
        )
        maintainers = extra_section.get("recipe-maintainers", [])

        if recipe_version == 1:
            recipe_name = conda_recipe_v1_linter.get_recipe_name(meta)
        else:
            recipe_name = package_section.get("name", "").strip()

        # 4. Check for existing feedstocks in conda-forge or bioconda
        if recipe_name:
            cf = gh.get_user("conda-forge")

            for name in set(
                [
                    recipe_name,
                    recipe_name.replace("-", "_"),
                    recipe_name.replace("_", "-"),
                ]
            ):
                try:
                    if cf.get_repo(f"{name}-feedstock"):
                        existing_recipe_name = name
                        feedstock_exists = True
                        break
                    else:
                        feedstock_exists = False
                except github.UnknownObjectException:
                    feedstock_exists = False

            if feedstock_exists and existing_recipe_name == recipe_name:
                lints[fname].append("Feedstock with the same name exists in conda-forge.")
            elif feedstock_exists:
                hints[fname].append(
                    f"Feedstock with the name {existing_recipe_name} exists in conda-forge. "
                    f"Is it the same as this package ({recipe_name})?"
                )

            bio = gh.get_user("bioconda").get_repo("bioconda-recipes")
            try:
                bio.get_dir_contents(f"recipes/{recipe_name}")
            except github.UnknownObjectException:
                pass
            else:
                hints[fname].append(
                    "Recipe with the same name exists in bioconda: "
                    "please discuss with @conda-forge/bioconda-recipes."
                )

            url = None
            if recipe_version == 1:
                for source_url in sources_section:
                    if source_url.startswith("https://pypi.io/packages/source/"):
                        url = source_url
            else:
                for source_section in sources_section:
                    if str(source_section.get("url")).startswith(
                        "https://pypi.io/packages/source/"
                    ):
                        url = source_section["url"]
            if url:
                # get pypi name from  urls like "https://pypi.io/packages/source/b/build/build-0.4.0.tar.gz"
                pypi_name = url.split("/")[6]
                mapping_request = requests.get(
                    "https://raw.githubusercontent.com/regro/cf-graph-countyfair/master/mappings/pypi/name_mapping.yaml"
                )
                if mapping_request.status_code == 200:
                    mapping_raw_yaml = mapping_request.content
                    mapping = get_yaml().load(mapping_raw_yaml)
                    for pkg in mapping:
                        if pkg.get("pypi_name", "") == pypi_name:
                            conda_name = pkg["conda_name"]
                            hints[fname].append(
                                f"A conda package with same name ({conda_name}) already exists."
                            )

        # 5. Ensure all maintainers have commented that they approve of being listed
        if maintainers:
            # Get PR author, issue comments, and review comments
            pr_author = pr.user.login
            issue_comments = pr.get_issue_comments()
            review_comments = pr.get_reviews()

            # Combine commenters from both issue comments and review comments
            commenters = {comment.user.login for comment in issue_comments}
            commenters.update({review.user.login for review in review_comments})

            # Check if all maintainers have either commented or are the PR author
            non_participating_maintainers = set()
            for maintainer in maintainers:
                if maintainer not in commenters and maintainer != pr_author:
                    non_participating_maintainers.add(maintainer)

            # Add a lint message if there are any non-participating maintainers
            if non_participating_maintainers:
                lints[fname].append(
                    f"The following maintainers have not yet confirmed that they are willing to be listed here: "
                    f"{', '.join(non_participating_maintainers)}. Please ask them to comment on this PR if they are."
                )

    return dict(lints), dict(hints)


def _comment_on_pr(pr, lints, hints):
    if lints:
        topline = "I found some lint."
    elif hints:
        topline = "your PR looks excellent but I have some suggestions."
    else:
        topline = "your PR looks excellent! :rocket:"
    summary = f"Hi! This is the staged-recipes linter and {topline}\n"

    all_fnames = set(lints.keys()) | set(hints.keys())
    for fname in all_fnames:
        lint_message = ""
        hint_message = ""

        if fname in lints and lints[fname]:
            lint_message = "**lints**\n"
            for lint in lints[fname]:
                if lint:
                    lint_message += f"- {lint}\n"

        if fname in hints and hints[fname]:
            hint_message = "**hints**\n"
            for hint in hints[fname]:
                if hint:
                    hint_message += f"- {hint}\n"

        if lint_message or hint_message:
            summary += f"#### {fname}:\n"
            summary += lint_message + hint_message + "\n"

    print(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Lint staged recipes.')
    parser.add_argument('--pr-num', type=int, required=True, help='the PR number')

    args = parser.parse_args()

    gh = github.Github(auth=github.Auth.Token(os.getenv("GH_TOKEN")))
    repo = gh.get_repo("conda-forge/staged-recipes")
    pr = repo.get_pull(args.pr_num)

    lints, hints = _lint_recipes(gh, pr)
    _comment_on_pr(pr, lints, hints)
    if lints:
        sys.exit(1)

