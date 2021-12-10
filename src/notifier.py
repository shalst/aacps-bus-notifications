import requests
import json
from pprint import pprint
from twilio.rest import Client
from configparser import ConfigParser
import pathlib
from datetime import datetime
import re
import os
from argparse import ArgumentParser


def format_notification(row, school, col_map):
    """
    Formats the notification message for the user.
    """
    bus = f"Bus # -- {row[col_map['bus']]}"
    school = f"School -- {school}"
    sub = row[col_map["sub bus"]] if row[col_map["sub bus"]].strip() != "" else "NO SUB!"
    sub_bus = f"Sub # -- {sub}"
    time_slot = f"Time -- {row[col_map['schedules']]}"
    impact = f"Impact -- {row[col_map['impact']].upper()}"
    return "\n\n".join(
        ["Affected Bus:", bus, time_slot, school, sub_bus, impact]
    ).strip()


def reverse_notification(notification):
    """
    Parses a notification and turns it into a notice of bus cancellation cancellation.
    """
    notifs = notification.split("\n\n")[1:-1]
    return "\n\n".join(["Bus is now running:", *notifs])


def validate_data(raw_data):
    """
    Finds the table schema in the website, validates necessary columns are there, and returns a mapping from column name to column index.
    """
    data = raw_data.replace("\n", "").replace("\r", "").replace("\t", "")
    cols = json.loads(
        ("[" + ", ".join(re.findall(r"columns: \[(.*?)\]", data)) + "]").lower()
    )
    cols = [col["title"].strip() for col in cols]
    cols_map = {col: cols.index(col) for col in cols}
    return (
        cols_map,
        "bus" in cols
        and "sub bus" in cols
        and "schools" in cols
        and "schedules" in cols
        and "impact" in cols,
    )


def get_number_iterator(current_dir, configs):
    """
    Reads the phone numbers currently just stored in a CSV and returns an iterator of all user entries
    """
    if os.path.exists(current_dir / configs["general"]["users"]):
        with open(current_dir / configs['general']['users'], "r") as recipients:
            users = [
                (
                    r.split("|")[0],
                    r.split("|")[1],
                    r.split("|")[2],
                    r.split("|")[3] if len(r.split("|")) > 3 else "F",
                )
                for r in recipients.read().split("\n")[1:]
                if len(r.split("|")) >= 3
            ]
        return users
    else:
        return []


def create_notification(phone_number, bus_number, school, always_notify, bus_map):
    """
    Returns a pair with phone number of current message (if any) to send
    """
    always_notify = always_notify.lower() == "t"
    for message in bus_map.get(bus_number, []):
        if school in message.lower():
            return (phone_number, message)
    if not bus_map.get(bus_number, []) and always_notify:
        school_line = f" {school.title()} " if school != "" else " "
        message = f"Bus {bus_number}{school_line}is running as scheduled."
        return (phone_number, message)
    return (None, None)


def notify_users_map(raw_data, current_dir, configs, logging=True):
    """
    Create a mapping from each phone number to a list of messages to send to that phone number.
    """
    text_map = dict()
    always_text_map = dict()
    if logging:
        # log current schedule
        logs_dir = current_dir / configs['general']['logs_dir']
        log_file = (
            logs_dir / f"{datetime.now().strftime('%d-%m-%Y-%H-%M-%S')}-logs.html"
        )
        with open(log_file, "w") as log:
            log.write(raw_data.strip().replace("\r", ""))

        # delete old logs past threshold
        logs = [
            logs_dir / log
            for log in os.listdir(logs_dir)
            if log.split(".")[-1] == "html"
        ]
        if len(logs) >= int(configs["general"]["log_threshold"]):
            oldest_file = min(logs, key=os.path.getctime)
            os.remove(os.path.abspath(oldest_file))

    col_map, valid_data = validate_data(raw_data)
    if valid_data:
        # Welcome to the most dense, unpythonic code possible.
        # I know.  Sorry.
        # Extracts the table information from AACPS' bus website.
        data = json.loads(
            next(
                filter(
                    lambda line: "var dataArray" in line,
                    raw_data.split(
                        "\n"
                    ),  # returns the line of the raw HTML that contains the table data
                )
            )
            .split("=")[-1]  # drops the "var dataArray = " part of the line
            .strip()
            .replace(";", "")  # removes the trailing semicolon
            .replace(
                "'", '"'
            )  # replaces single quotes with double quotes so it can be jsonified
        )

        # create a mapping from bus number to all outages for that particular bus
        message_map = dict()
        for row in data:
            message_map[row[col_map["bus"]]] = message_map.get(
                row[col_map["bus"]], []
            ) + [format_notification(row, row[col_map["schools"]], col_map)]

        # iterate over every recipient listed in the recipients file and send notification it here is an outage
        for phone_num, bus_num, school, always_notify in get_number_iterator(current_dir, configs):
            phone_num, text = create_notification(
                phone_num, bus_num, school, always_notify, message_map
            )
            if (phone_num is not None) and (text is not None):
                if always_notify.lower() == "t":
                    always_text_map[phone_num] = always_text_map.get(phone_num, []) + [
                        text
                    ]
                else:
                    text_map[phone_num] = text_map.get(phone_num, []) + [text]
    else:
        text_map[configs["debug"]["to_phone"]] = [
            f"Error: Table does not have proper schema.\n\n{col_map.keys()}"
        ]
    return text_map, always_text_map


def send_text_messages(text_mapping, call_client, configs, prefix=""):
    """
    Take a mapping from phone numbers to list of message and send each message to their corresponding phone number
    """
    separator = " - " if (prefix != "") else ""
    for phone_num, messages in text_mapping.items():
        for message in messages:
            try:
                call_client.messages.create(
                    body=prefix + separator + message,
                    from_=configs["twilio"]["from_phone"],
                    to=phone_num,
                )
            except Exception as e:
                print(f">>> Error: {e}")
                call_client.messages.create(
                    body=f"Bus Error: {e} / Phone: {phone_num} / Message: {message}",
                    from_=configs["debug"]["from_phone"],
                    to=configs["debug"]["to_phone"],
                )


def filter_texts(raw_texts_to_send, configs, compare=False):
    """
    If comparing, then we want to compare the current schedule to the previous schedule and adjust messages to only send new information.
    """
    filtered_texts = dict()
    old_texts_location = current_dir / configs["general"]["resources"] / configs["general"]["logged_texts"]
    if os.path.exists(old_texts_location) and compare:
        with open(old_texts_location, "r") as old_texts_file:
            previous_texts_sent = json.load(old_texts_file)
        for phone_num in set(raw_texts_to_send.keys()).union(
            set(previous_texts_sent.keys())
        ):
            new_texts = set(raw_texts_to_send.get(phone_num, []))
            old_texts = set(previous_texts_sent.get(phone_num, []))
            if set(texts.lower() for texts in new_texts) != set(
                texts.lower() for texts in old_texts
            ):
                new_bus_shortage = list(new_texts - old_texts)
                old_bus_reversal = old_texts - new_texts
                old_bus_reversal = list(map(reverse_notification, old_bus_reversal)) if old_bus_reversal else []
                filtered_texts[phone_num] = new_bus_shortage + old_bus_reversal
        return filtered_texts
    else:
        return raw_texts_to_send


if __name__ == "__main__":
    current_dir = pathlib.Path(__file__).parent

    # read configs
    configs = ConfigParser()
    configs.read(current_dir / "configs.properties")
    logs_dir = current_dir / configs["general"]["logs_dir"]
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir, exist_ok=True)

    # check args
    parser = ArgumentParser(description="AACPS Bus Outage Notifier")
    parser.add_argument(
        "-l", "--log", action="store_true", help="Logs the current schedule"
    )
    parser.add_argument(
        "-c",
        "--compare",
        action="store_true",
        help="Compares the current schedule to the previous one to determine messages to send",
    )
    parser.add_argument(
        "-p",
        "--prefix",
        type=str,
        help="Prefix string to beginning of all messages"
    )
    args = parser.parse_args()

    # create Twilio client and get the current schedule
    call_client = Client(
        os.environ[configs["twilio"]["sid"]], os.environ[configs["twilio"]["auth"]]
    )
    raw_data = requests.get(configs["general"]["site"]).text

    # notify them peeps dawg
    raw_texts_to_send, always_raw_texts = notify_users_map(
        raw_data, current_dir, configs, args.log
    )
    texts_to_send = filter_texts(raw_texts_to_send, configs, args.compare)
    send_text_messages(texts_to_send, call_client, configs, args.prefix)
    print("*** Normal Texts Sent ***")
    pprint(texts_to_send)
    send_text_messages(always_raw_texts, call_client, configs, args.prefix)
    print("*** Always Texts Sent ***")
    pprint(always_raw_texts)

    # save current sent logs
    with open(current_dir / configs["general"]["resources"] / configs["general"]["logged_texts"], "w") as text_file:
        json.dump(raw_texts_to_send, text_file, indent=4)
