#!/usr/bin/env python
"""
This program lets you do archive slack channels which are no longer active.
"""

from datetime import datetime
import os
import sys
import time
import json

import requests

from config import get_channel_reaper_settings
from utils import get_logger


class ChannelReaper:
    """
    This class can be used to archive slack channels.
    """

    def __init__(self):
        self.settings = get_channel_reaper_settings()
        self.logger = get_logger("channel_reaper", "./audit.log")

    def get_whitelist_keywords(self):
        """
        Get all whitelist keywords. If this word is used in the channel
        purpose or topic, this will make the channel exempt from archiving.
        """
        keywords = []
        if os.path.isfile("whitelist.txt"):
            with open("whitelist.txt") as filecontent:
                keywords = filecontent.readlines()

        # remove whitespace characters like `\n` at the end of each line
        keywords = map(lambda x: x.strip(), keywords)
        whitelist_keywords = self.settings.get("whitelist_keywords")
        if whitelist_keywords:
            keywords = keywords + whitelist_keywords.split(",")
        return list(keywords)

    def get_channel_alerts(self):
        """Get the alert message which is used to notify users in a channel of archival."""
        with open("templates.json") as filecontent:
            alerts = json.load(filecontent)
        return alerts

    def slack_api_http(
        self,
        api_endpoint=None,
        payload=None,
        method="GET",
        retry_delay=0,
        as_bot=False,
    ):
        """Helper function to query the slack api and handle errors and rate limit."""
        uri = "https://slack.com/api/" + api_endpoint
        token = self.settings.get("bot_slack_token") if as_bot else self.settings.get("slack_token")
        headers = {
            "Authorization": f"Bearer {token}",
        }
        try:
            # Force request to take at least 1 second. Slack docs state:
            # > In general we allow applications that integrate with Slack to send
            # > no more than one message per second. We allow bursts over that
            # > limit for short periods.
            if retry_delay > 0:
                time.sleep(retry_delay)

            if method == "POST":
                response = requests.post(uri, data=payload, headers=headers)
            else:
                response = requests.get(uri, params=payload, headers=headers)

            if (
                response.status_code == requests.codes.ok
                and "error" in response.json()
                and response.json()["error"] == "not_authed"
            ):
                self.logger.error(
                    "Need to setup auth. eg, SLACK_TOKEN=<secret token> python slack-autoarchive.py"
                )
                sys.exit(1)
            elif response.status_code == requests.codes.ok and response.json()["ok"]:
                return response.json()
            elif response.status_code == requests.codes.too_many_requests:
                retry_timeout = float(response.headers["Retry-After"])
                return self.slack_api_http(api_endpoint, payload, method, retry_timeout)
        except Exception as error_msg:
            raise Exception(error_msg)
        return None

    def get_all_channels(self):
        """Get a list of all non-archived channels from slack channels.list."""
        # TODO: Add support for more than 1000 channels.
        payload = {
            "exclude_archived": 1,
            "limit": 1000,
        }
        api_endpoint = "conversations.list"
        channels = self.slack_api_http(api_endpoint=api_endpoint, payload=payload)[
            "channels"
        ]
        all_channels = []
        for channel in channels:
            all_channels.append(
                {
                    "id": channel["id"],
                    "name": channel["name"],
                    "created": channel["created"],
                    "num_members": channel["num_members"],
                    "topic": channel["topic"]["value"],
                    "purpose": channel["purpose"]["value"],
                }
            )
        return all_channels

    def get_last_message_timestamp(self, channel_history, too_old_datetime):
        """Get the last message from a slack channel, and return the time."""
        last_message_datetime = too_old_datetime

        if not channel_history:
            return datetime.utcfromtimestamp(0)

        if "messages" not in channel_history:
            return last_message_datetime  # no messages

        for message in channel_history["messages"]:
            if "subtype" in message and message["subtype"] in self.settings.get(
                "skip_subtypes"
            ):
                continue
            last_message_datetime = datetime.fromtimestamp(float(message["ts"]))
            break

        # for folks with the free plan, sometimes there is no last message,
        # then just set last_message_datetime to epoch
        if not last_message_datetime:
            last_message_datetime = datetime.utcfromtimestamp(0)

        return last_message_datetime

    def is_channel_disused(self, channel, too_old_datetime):
        """Return True or False depending on if a channel is "active" or not."""
        num_members = channel["num_members"]
        payload = {"inclusive": 0, "oldest": 0, "count": 50}
        api_endpoint = "conversations.history"

        payload["channel"] = channel["id"]
        channel_history = self.slack_api_http(
            api_endpoint=api_endpoint, payload=payload
        )
        last_message_datetime = self.get_last_message_timestamp(
            channel_history, datetime.fromtimestamp(float(channel["created"]))
        )

        last_message_is_too_old = last_message_datetime <= too_old_datetime
        min_members = self.settings.get("min_members")
        has_min_users = min_members == 0 or min_members > num_members

        return last_message_is_too_old and has_min_users

    # If you add channels to the WHITELIST_KEYWORDS constant they will be exempt from archiving.
    def is_channel_whitelisted(self, channel, white_listed_channels):
        """Return True or False depending on if a channel is exempt from being archived."""
        channel_purpose = channel["purpose"]
        channel_topic = channel["topic"]
        if (
            self.settings.get("skip_channel_str") in channel_purpose
            or self.settings.get("skip_channel_str") in channel_topic
        ):
            return True

        # check the white listed channels (file / env)
        for white_listed_channel in white_listed_channels:
            wl_channel_name = white_listed_channel.strip("#")
            if wl_channel_name in channel["name"]:
                return True
        return False

    def send_channel_message(self, channel_id, message):
        """Send a message to a channel or user."""
        payload = {
            "channel": channel_id,
            "username": "Auto Archiver",
            "icon_emoji": ":wave:",
            "text": message,
        }
        api_endpoint = "chat.postMessage"
        self.slack_api_http(api_endpoint=api_endpoint, payload=payload, method="POST", as_bot=True)

    def archive_channel(self, channel, alert):
        """Archive a channel, and send alert to slack admins."""
        api_endpoint = "conversations.archive"
        stdout_message = "Archiving channel... %s" % channel["name"]
        self.logger.info(stdout_message)

        if not self.settings.get("dry_run"):
            channel_message = alert.format(days_inactive=self.settings.get("days_inactive"))
            self.send_channel_message(channel["id"], channel_message)
            payload = {"channel": channel["id"]}
            self.slack_api_http(api_endpoint=api_endpoint, payload=payload)
            self.logger.info(stdout_message)

    def send_admin_report(self, channels):
        """Optionally this will message admins with which channels were archived."""
        if self.settings.get("admin_channel"):
            channel_names = ", ".join("#" + channel["name"] for channel in channels)
            admin_msg = "Archiving %d channels: %s" % (len(channels), channel_names)
            if self.settings.get("dry_run"):
                admin_msg = "[DRY RUN] %s" % admin_msg
            self.send_channel_message(self.settings.get("admin_channel"), admin_msg)

    def generate_report(self, channels):
        """Generate a report. Usual for a dry-run or after a run."""
        file_name = f'report-{datetime.now().strftime("%m%d%Y%H%M%S")}.txt'
        with open(file_name, "w") as f:
            f.write("\n".join(str(channel["name"]) for channel in channels))

    def main(self):
        """
        This is the main method that checks all inactive channels and archives them.
        """
        if self.settings.get("dry_run"):
            self.logger.info("THIS IS A DRY RUN. NO CHANNELS ARE ACTUALLY ARCHIVED.")

        whitelist_keywords = self.get_whitelist_keywords()
        alert_templates = self.get_channel_alerts()
        archived_channels = []

        for channel in self.get_all_channels():
            sys.stdout.write(".")
            sys.stdout.flush()

            channel_whitelisted = self.is_channel_whitelisted(
                channel, whitelist_keywords
            )
            channel_disused = self.is_channel_disused(
                channel, self.settings.get("too_old_datetime")
            )
            if not channel_whitelisted and channel_disused:
                archived_channels.append(channel)
                self.archive_channel(channel, alert_templates["channel_template"])

        self.send_admin_report(archived_channels)
        self.generate_report(archived_channels)


if __name__ == "__main__":
    CHANNEL_REAPER = ChannelReaper()
    CHANNEL_REAPER.main()
