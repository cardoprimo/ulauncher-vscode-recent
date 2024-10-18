import os
import os.path
import json
import logging
import pathlib
import sqlite3
import urllib
import subprocess

from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import (
    KeywordQueryEvent,
    ItemEnterEvent,
    PreferencesEvent,
    PreferencesUpdateEvent,
)
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.HideWindowAction import HideWindowAction
from ulauncher.api.shared.action.ExtensionCustomAction import ExtensionCustomAction
import re
import time

logger = logging.getLogger(__name__)


# Command score function
def command_score(string, abbreviation, aliases=None):
    if aliases is None:
        aliases = []

    # Score constants
    SCORE_CONTINUE_MATCH = 1
    SCORE_SPACE_WORD_JUMP = 0.9
    SCORE_NON_SPACE_WORD_JUMP = 0.8
    SCORE_CHARACTER_JUMP = 0.17

    PENALTY_SKIPPED = 0.999
    PENALTY_CASE_MISMATCH = 0.9999

    # Regular expressions for matching gaps and spaces
    IS_GAP_REGEXP = re.compile(r'[\\\/_+.#"@\[\(\{&]')
    IS_SPACE_REGEXP = re.compile(r"[\s-]")

    # Convert string and aliases to lowercase
    lower_string = (string + " " + " ".join(aliases)).lower()
    lower_abbreviation = abbreviation.lower()

    # Recursive function to calculate score
    def score(string_index, abbr_index, memo=None):
        if memo is None:
            memo = {}

        # Memoization key
        memo_key = (string_index, abbr_index)
        if memo_key in memo:
            return memo[memo_key]

        # Base case: if we have matched all abbreviation characters
        if abbr_index == len(abbreviation):
            return SCORE_CONTINUE_MATCH if string_index == len(string) else 0.99

        # Find the next matching character in the string
        abbreviation_char = lower_abbreviation[abbr_index]
        high_score = 0
        index = lower_string.find(abbreviation_char, string_index)

        # Loop through possible matches
        while index != -1:
            temp_score = score(index + 1, abbr_index + 1, memo)

            # Continuous match
            if index == string_index:
                temp_score *= SCORE_CONTINUE_MATCH
            # Word boundary match
            elif IS_SPACE_REGEXP.match(lower_string[index - 1]):
                temp_score *= SCORE_SPACE_WORD_JUMP
            elif IS_GAP_REGEXP.match(lower_string[index - 1]):
                temp_score *= SCORE_NON_SPACE_WORD_JUMP
            # Character jump
            else:
                temp_score *= SCORE_CHARACTER_JUMP
                if string_index > 0:
                    temp_score *= PENALTY_SKIPPED ** (index - string_index)

            # Case mismatch penalty
            if string[index] != abbreviation[abbr_index]:
                temp_score *= PENALTY_CASE_MISMATCH

            # Update the best score
            if temp_score > high_score:
                high_score = temp_score

            # Look for the next match in the string
            index = lower_string.find(abbreviation_char, index + 1)

        # Memoize the result
        memo[memo_key] = high_score
        return high_score

    # Start the scoring from the first character
    return score(0, 0)


class Utils:
    @staticmethod
    def get_path(filename, from_home=False):
        base_dir = (
            pathlib.Path.home()
            if from_home
            else pathlib.Path(__file__).parent.absolute()
        )
        return os.path.join(base_dir, filename)


class Code:
    path_dirs = ("/usr/bin", "/bin", "/snap/bin")
    variants = ("Code", "Codium")

    # Cache for recent entries and cache expiry
    _cached_recents = None
    _cache_timestamp = 0
    _cache_duration = 60  # Cache duration in seconds

    def __init__(self):
        self.installed_path = None
        self.config_path = None
        self.global_state_db = None
        self.storage_json = None
        self.include_types = None
        self.prefer_type = None

        logger.debug("locating installation and config directories")
        for path in (pathlib.Path(path_dir) for path_dir in Code.path_dirs):
            for variant in Code.variants:
                installed_path = path / variant.lower()
                if not installed_path.exists():
                    continue
                if variant == "Codium":
                    variant_config = "VSCodium"
                else:
                    variant_config = variant
                config_path = pathlib.Path.home() / ".config" / variant_config
                logger.debug(
                    "evaluating installation dir %s and config dir %s",
                    installed_path,
                    config_path,
                )
                if (
                    installed_path.exists()
                    and config_path.exists()
                    and (
                        config_path / "User" / "globalStorage" / "storage.json"
                    ).exists()
                ):
                    logger.debug(
                        "found installation dir %s and config dir %s",
                        installed_path,
                        config_path,
                    )
                    self.installed_path = installed_path
                    self.config_path = config_path
                    self.global_state_db = (
                        config_path / "User" / "globalStorage" / "state.vscdb"
                    )
                    self.storage_json = (
                        config_path / "User" / "globalStorage" / "storage.json"
                    )
                    return

        logger.warning("Unable to find VS Code installation and config directory")

    def is_installed(self):
        return bool(self.installed_path)

    def get_recents(self):
        # Check if we have valid cached recents
        current_time = time.time()
        if (
            Code._cached_recents
            and current_time - Code._cache_timestamp < Code._cache_duration
        ):
            logger.debug("Returning cached recents")
            return Code._cached_recents

        # Fetch recents from global state or legacy storage if cache is expired
        recents = []

        include_types = self.include_types
        prefer_type = self.prefer_type

        if self.global_state_db.exists():
            logger.debug("getting recents from global state database")
            try:
                recents = self.get_recents_global_state()
            except Exception as e:
                logger.error("getting recents from global state database failed", e)
                if not self.storage_json.exists():
                    raise e

        if not recents and self.storage_json.exists():
            logger.debug("getting recents from storage.json (legacy)")
            recents = self.get_recents_legacy()

        # Update the cache
        Code._cached_recents = recents
        Code._cache_timestamp = time.time()

        return recents

    def get_recents_global_state(self):
        logger.debug("connecting to global state database %s", self.global_state_db)
        con = sqlite3.connect(self.global_state_db)
        cur = con.cursor()
        cur.execute(
            'SELECT value FROM ItemTable WHERE key = "history.recentlyOpenedPathsList"'
        )
        (json_code,) = cur.fetchone()
        paths_list = json.loads(json_code)
        entries = paths_list["entries"]
        include_types = self.include_types
        logger.debug("found %d entries in global state database", len(entries))
        return self.parse_entry_paths(entries, include_types)

    def get_recents_legacy(self):
        """
        For Visual Studio Code Pre versions before 1.64
        :uri https://code.visualstudio.com/updates/v1_64
        """
        logger.debug("loading storage.json")
        storage = json.load(self.storage_json.open("r"))
        entries = storage["openedPathsList"]["entries"]
        include_types = self.include_types
        logger.debug("found %d entries in storage.json", len(entries))
        return self.parse_entry_paths(entries, include_types)

    @staticmethod
    def parse_entry_paths(entries, include_types):
        recents = []
        for path in entries:

            if "folderUri" in path:
                uri = path["folderUri"]
                icon = "icon"
                option = "--folder-uri"
                entry_type = "folder"
            elif "fileUri" in path:
                uri = path["fileUri"]
                icon = "file"
                option = "--file-uri"
                entry_type = "file"
            elif "workspace" in path:
                uri = path["workspace"]["configPath"]
                icon = "workspace"
                option = "--file-uri"
                entry_type = "workspace"
            else:
                logger.warning("entry not recognized: %s", path)
                continue

            label = path["label"] if "label" in path else uri.split("/")[-1]
            recents.append(
                {
                    "uri": uri,
                    "label": label,
                    "icon": icon,
                    "option": option,
                    "type": entry_type,
                }
            )

        logger.debug('included types: %s' % include_types)
        # filter the entries to only include types of the preferences["include_types"]

        recents = [recent for recent in recents if recent["type"] in include_types]
        return recents

    def open_vscode(self, recent, excluded_env_vars):
        if not self.is_installed():
            return
        # Get the current environment variables
        current_env = os.environ.copy()

        # Remove the environment variables that we don't want to pass to the new process if any
        if excluded_env_vars:
            for env_var in excluded_env_vars.split(","):
                env_to_exclude = env_var.strip()
                if env_to_exclude in current_env:
                    del current_env[env_to_exclude]

        # Start the new process with the modified environment
        subprocess.run(
            [self.installed_path, recent["option"], recent["uri"]], env=current_env
        )


class CodeExtension(Extension):
    keyword = None
    excluded_env_vars = None
    code = None
    include_types = None
    prefer_type = None

    def __init__(self):
        super(CodeExtension, self).__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(ItemEnterEvent, ItemEnterEventListener())
        self.subscribe(PreferencesEvent, PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent, PreferencesUpdateEventListener())
        self.code = Code()
        self.home_path = str(pathlib.Path.home())

    def get_pretty_dir_path(self, path):
        # get th epretty printed path
        path = urllib.parse.unquote(path)
        path = path.replace("file://", "")

        return path.replace(self.home_path, "~")

    def get_ext_result_items(self, query):

        query = query.lower() if query else ""
        recents = self.code.get_recents()
        items = []
        data = []
        prefer_type = self.prefer_type

        logger.debug("prefered type: %s", prefer_type)

        # Use command_score instead of fuzzywuzzy for scoring the label and URI
        for recent in recents:
            label_score = command_score(recent["label"], query)
            uri_score = command_score(recent["uri"], query)


            # prefer types
            if prefer_type and recent["type"] == prefer_type:
                label_score *= 1.02  # increase score by 2% for workspaces


            # Only add items that have a score above a threshold
            if label_score > 0.1 or uri_score > 0.1:
                data.append({"recent": recent, "score": max(label_score, uri_score)})

        # Sort the results by the score, highest first
        data = sorted(data, key=lambda x: x["score"], reverse=True)

        for recent_item in data[:15]:
            recent = recent_item["recent"]

            # get th epretty printed path
            path = self.get_pretty_dir_path(recent["uri"])
            items.append(
                ExtensionResultItem(
                    icon=Utils.get_path(f"images/{recent['icon']}.svg"),
                    name=urllib.parse.unquote(recent["label"]),
                    description=path,
                    on_enter=ExtensionCustomAction(recent),
                )
            )

        return items


class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        items = []

        if not extension.code.is_installed():
            items.append(
                ExtensionResultItem(
                    icon=Utils.get_path("images/icon.svg"),
                    name="No VS Code?",
                    description="Can't find the VS Code's `code` command in your system :(",
                    highlightable=False,
                    on_enter=HideWindowAction(),
                )
            )
            return RenderResultListAction(items)

        argument = event.get_argument() or ""
        items.extend(extension.get_ext_result_items(argument))
        return RenderResultListAction(items)


class ItemEnterEventListener(EventListener):
    def on_event(self, event, extension):
        recent = event.get_data()
        extension.code.open_vscode(recent, extension.excluded_env_vars)


class PreferencesEventListener(EventListener):
    def on_event(self, event, extension):
        extension.keyword = event.preferences["code_kw"]
        extension.excluded_env_vars = event.preferences["excluded_env_vars"]
        extension.code.include_types = event.preferences["include_types"].split(",")
        extension.prefer_type = event.preferences["prefer_type"]


class PreferencesUpdateEventListener(EventListener):
    def on_event(self, event, extension):
        if event.id == "code_kw":   
            extension.keyword = event.new_value
        if event.id == "excluded_env_vars":
            extension.excluded_env_vars = event.new_value
        if event.id == "include_types":
            extension.code.include_types = event.new_value.split(",")
        if event.id == "prefer_type":
            extension.prefer_type = event.new_value


if __name__ == "__main__":
    CodeExtension().run()
