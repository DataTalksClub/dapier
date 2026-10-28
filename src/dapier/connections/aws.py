"""AWS clients assuming an operator-configured IAM role."""

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
    if not config.get("role_arn"):
        raise ValueError(f"credential {credential_id} holds no role_arn")
    if config["role_arn"] not in allowed_role_arns():
        raise ValueError("AWS role is not in the deployment's allowed role list")
    return config


def client(service, config):
    """Assume the stored role with the runtime identity; never persist
    temporary credentials."""
    region = config.get("region") or os.environ.get("AWS_REGION") or "eu-west-1"
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
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=temporary["AccessKeyId"],
        aws_secret_access_key=temporary["SecretAccessKey"],
        aws_session_token=temporary["SessionToken"],
    )
