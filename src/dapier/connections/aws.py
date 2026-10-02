"""AWS clients using an operator-selected role or legacy stored keys."""

import os

import boto3

from . import credentials


def allowed_role_arns():
    return {
        arn.strip()
        for arn in os.environ.get("AWS_ASSUMABLE_ROLE_ARNS", "").split(",")
        if arn.strip()
    }


def stored_config(credential_id="aws"):
    try:
        config = credentials.get_credential(credential_id)
    except KeyError:
        raise ValueError(f"credential {credential_id} is not configured") from None
    if config.get("role_arn"):
        if config["role_arn"] not in allowed_role_arns():
            raise ValueError("AWS role is not in the deployment's allowed role list")
    elif not config.get("access_key_id") or not config.get("secret_access_key"):
        raise ValueError(f"credential {credential_id} does not contain AWS keys")
    return config


def client(service, config):
    """Assume with the runtime identity; never persist temporary credentials."""
    region = config.get("region") or os.environ.get("AWS_REGION") or "eu-west-1"
    if config.get("role_arn"):
        request = {
            "RoleArn": config["role_arn"],
            "RoleSessionName": "dapier-provider",
            "DurationSeconds": 900,
        }
        if config.get("external_id"):
            request["ExternalId"] = config["external_id"]
        temporary = boto3.client("sts", region_name=region).assume_role(**request)[
            "Credentials"
        ]
        options = {
            "aws_access_key_id": temporary["AccessKeyId"],
            "aws_secret_access_key": temporary["SecretAccessKey"],
            "aws_session_token": temporary["SessionToken"],
        }
    else:
        options = {
            "aws_access_key_id": config["access_key_id"],
            "aws_secret_access_key": config["secret_access_key"],
        }
    return boto3.client(service, region_name=region, **options)
