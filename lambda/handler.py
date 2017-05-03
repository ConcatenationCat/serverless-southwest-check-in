#
# handler.py
# Lamba functions for scheduling check-ins via the Southwest API
#

import json
import logging
import os
import sys

import boto3

# Add vendored dependencies to path
sys.path.append('./vendor')

from lib import swa, email, exceptions  # NOQA

# Set up logging
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


def schedule_check_in(event, context):
    """
    Looks up a reservation using the Southwest API and returns the available
    check-in times as a descending list.

    Returns:
      {'check_in_times': {'remaining': ['check_in2', 'check_in1']}}

    """

    # We already have the check-in times, just schedule the next one.
    if 'check_in_times' in event:
        event['check_in_times']['next'] = \
            event['check_in_times']['remaining'].pop()
        return event

    # New check-in, fetch reservation
    first_name = event['first_name']
    last_name = event['last_name']
    confirmation_number = event['confirmation_number']

    event['check_in_times'] = {}

    log.info("Looking up reservation {} for {} {}".format(confirmation_number,
                                                          first_name, last_name))
    reservation = swa.get_reservation(first_name, last_name, confirmation_number)
    log.debug("Reservation: {}".format(reservation))

    event['check_in_times']['remaining'] = \
        swa.get_check_in_times_from_reservation(reservation)

    # Call ourself now that we have some check-in times.
    return schedule_check_in(event, None)


def check_in(event, context):
    """
    TODO(dw): Fix description
    Retrieves reservations which are ready to be checked in from DynamoDB and
    checks them in via the Southwest API
    """

    first_name = event['first_name']
    last_name = event['last_name']
    confirmation_number = event['confirmation_number']
    email = event.get('email')

    log.info("Checking in {} {} ({})".format(first_name, last_name,
                                             confirmation_number))

    try:
        resp = swa.check_in(first_name, last_name, confirmation_number)
        log.info("Checked in {} {}!".format(first_name, last_name))
        log.debug("Check-in response: {}".format(resp))
    except Exception as e:
        log.error("Error checking in: {}".format(e))
        raise

    if email:
        log.info("Emailing boarding pass to {}".format(email))
        try:
            swa.email_boarding_pass(
                first_name, last_name, confirmation_number, email
            )
        except Exception as e:
            log.error("Error emailing boarding pass: {}".format(e))

    # Raise exception to schedule the next check-in
    # This is caught by AWS Step and then schedule_check_in is called again
    if len(event['check_in_times']['remaining']) > 0:
        raise exceptions.NotLastCheckIn()


def receive_email(event, context):
    sfn = boto3.client('stepfunctions')
    ses_notification = event['Records'][0]['ses']
    # ARN of the AWS Step State Machine to execute when an email
    # is successfully parsed and a new check-in should run.
    state_machine_arn = os.getenv('STATE_MACHINE_ARN')

    log.debug("State Machine ARN: {}".format(state_machine_arn))
    log.debug("SES Notification: {}".format(ses_notification))

    ses_msg = email.SesMailNotification(ses_notification['mail'])

    try:
        reservation = email.find_name_and_confirmation_number(ses_msg)
        log.info("Found reservation: {}".format(reservation))
    except Exception as e:
        log.error("Error scraping email {}: {}".format(ses_msg.message_id, e))
        return

    # Don't add the email if it's straight from southwest.com
    if not ses_msg.source.endswith('southwest.com'):
        reservation['email'] = ses_msg.source

    execution = sfn.start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps(reservation)
    )

    log.debug("State machine started at: {}".format(execution['startDate']))
    log.debug("Execution ARN: {}".format(execution['executionArn']))

    # Remove the startDate from the return value because datetime objects don't
    # easily serialize to JSON.
    del(execution['startDate'])

    return execution
