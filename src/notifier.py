import requests
import json
from pprint import pprint
import os
from twilio.rest import Client
from configparser import ConfigParser
import pathlib


def format_notification(row, school):
    bus = f"Bus # -- {row[1]}"
    school = f"School -- {school}"
    sub = row[2] if row[2].strip() != "" else "NO SUB!"
    sub_bus = f"Sub # -- {sub}"
    time_slot = f"Time -- {row[4]}"
    impact = f"Impact -- {row[5]}"
    return "\n\n".join(["Affected Bus:", bus, time_slot, school, sub_bus, impact])


def get_number_iterator(recipients_csv):
    # Reads the phone numbers currently just stored in a CSV
    with open(recipients_csv, "r") as recipients:
        users = [
            (r.split("|")[0], r.split("|")[1], r.split("|")[2], r.split("|")[3] if len(r.split("|")) > 3 else "true")
            for r in recipients.read().split("\n")[1:] if len(r.split("|")) >= 3
        ]
    return users


def send_notification(
    phone_number,
    bus_number,
    school,
    always_notify,
    bus_map,
    twilio_client,
    twilio_number,
):
    for message in bus_map.get(bus_number, []):
        if school in message.lower():
            notification = twilio_client.messages.create(
                body=message, from_=twilio_number, to=phone_number
            )
            print(f"<U> {phone_number}, <M> {message}")
    if not bus_map.get(bus_number, []) and always_notify == "true":
        school_line = f" {school.title()} " if school != "" else " "
        message = f"Bus {bus_number}{school_line}is running as scheduled."
        notification = twilio_client.messages.create(
            body=message,
            from_=twilio_number,
            to=phone_number,
        )
        print(f"<U> {phone_number}, <M> {message}")


if __name__ == "__main__":
    current_dir = pathlib.Path(__file__).parent
    configs, auths = ConfigParser(), ConfigParser()
    configs.read(current_dir / "configs.properties")
    auths.read(current_dir / "auth.properties")
    account_sid = auths["twilio"]["sid"]
    auth_token = auths["twilio"]["auth"]
    call_client = Client(account_sid, auth_token)

    # Welcome to the most dense, unpythonic code possible.
    # I know.  Sorry.
    # Extracts the table information from AACPS' bus website.
    data = json.loads(
        next(
            filter(
                lambda line: "var dataArray" in line,
                requests.get(
                    "https://busstops.aacps.org/public/BusRouteIssues.aspx"
                ).text.split("\n"),
            )
        )
        .split("=")[-1]
        .strip()[:-1]
        .replace("'", '"')
    )

    message_map = dict()
    for row in data:
        school = row[3]
        message_map[row[1]] = message_map.get(row[1], []) + [
            format_notification(row, school)
        ]

    for phone_num, bus_num, school, always_notify in get_number_iterator(
        current_dir / "recipients.csv"
    ):
        try:
            send_notification(
                phone_num,
                bus_num,
                school,
                always_notify,
                message_map,
                call_client,
                configs["twilio"]["from_phone"],
            )
        except Exception as e:
            print(f">>> Error: {e}")
            call_client.messages.create(
                body=f"Bus Error: {e}",
                from_=configs["debug"]["from_phone"],
                to=configs["debug"]["to_phone"],
            )
