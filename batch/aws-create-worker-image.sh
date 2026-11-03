#!/bin/bash

set -e

cd "$(dirname "$0")"
source ../devbin/functions.sh

if [ -z "${NAMESPACE}" ]; then
    echo "Must specify a NAMESPACE environment variable"
    exit 1;
fi

REGION=$(get_global_config_field aws_region $NAMESPACE)
DOCKER_ROOT_IMAGE=$(get_global_config_field docker_root_image $NAMESPACE)

# When you bump the WORKER_IMAGE_VERSION, you should also update:
# - the INSTANCE_VERSION in globals.py (add one to the current value)
# - the image name in batch/batch/cloud/gcp/driver/create_instance.py (should match this value)
WORKER_IMAGE_VERSION=33

if [ "$NAMESPACE" == "default" ]; then
    WORKER_IMAGE=batch-worker-${WORKER_IMAGE_VERSION}
    BUILDER=build-batch-worker-image
else
    WORKER_IMAGE=batch-worker-$NAMESPACE-${WORKER_IMAGE_VERSION}
    BUILDER=build-batch-worker-$NAMESPACE-image
fi

UBUNTU_IMAGE=/aws/service/canonical/ubuntu/server-minimal/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id

WORKER_IMAGE_EXISTS=false
WORKER_IMAGE_ID=$(aws ec2 describe-images --owners self --filters "Name=name,Values=${WORKER_IMAGE}" --query="Images[0].ImageId" --output text --region "${REGION}")
if [[ "$WORKER_IMAGE_ID" != "None" ]]; then
    WORKER_IMAGE_EXISTS=true
    if [ "$NAMESPACE" == "default" ]; then
        echo "ERROR: Image $WORKER_IMAGE already exists in region $REGION. Delete it first or bump WORKER_IMAGE_VERSION."
        exit 1
    else
        echo "WARNING: Image $WORKER_IMAGE already exists in region $REGION and will be overwritten."
    fi
fi

LEFTOVER_BUILDERS="$(aws ec2 describe-instances --region $REGION --filter="Name=tag:Name,Values=${BUILDER}" --query="Reservations[*].Instances[*].[InstanceId]" --output text | tr '\n' ' ')"
if [[ -n "$LEFTOVER_BUILDERS" ]]; then
    echo "WARNING: Found leftover builder VM(s) in region $REGION:"
    echo "$LEFTOVER_BUILDERS"
fi

create_build_image_instance() {
    if [[ -n "$LEFTOVER_BUILDERS" ]]; then
        aws ec2 terminate-instances --region $REGION --instance-ids ${LEFTOVER_BUILDERS}
    fi

    python3 ../ci/jinja2_render.py '{"global":{"docker_root_image":"'${DOCKER_ROOT_IMAGE}'"}}' \
        build-batch-worker-image-startup-aws.sh build-batch-worker-image-startup-aws.sh.out

    UBUNTU_AMI_ID=$(aws ssm get-parameter --name "$UBUNTU_IMAGE" --query="Parameter.Value" --output text --region ${REGION})
    SUBNET_ID=$(aws cloudformation list-exports --query="Exports[?Name=='VdcSubnet'].Value" --output text --region ${REGION})

    BUILDER_ID=$(aws ec2 run-instances \
        --image-id ${UBUNTU_AMI_ID} \
        --count 1 \
        --instance-type t3.large \
        --subnet-id ${SUBNET_ID} \
        --iam-instance-profile Name=worker-image-builder-hail-vdc \
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=10,VolumeType=gp3}" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value='${BUILDER}'}]" \
        --user-data file://build-batch-worker-image-startup-aws.sh.out \
	--region ${REGION} \
        --query "Instances[*].[InstanceId]" \
	--output text
    )
}

create_worker_image() {
    if [ "$WORKER_IMAGE_EXISTS" == "true" ]; then
        aws ec2 deregister-image --image-id $WORKER_IMAGE_ID --region $REGION
    fi

    NEW_IMAGE_ID=$(aws ec2 create-image \
        --instance-id ${BUILDER_ID} \
        --name ${WORKER_IMAGE} \
        --region ${REGION} \
        --query="ImageId" \
        --output text
    )

    while [ "$(aws ec2 describe-images --image-ids ${NEW_IMAGE_ID} --query='Images[0].State' --output text --region ${REGION})" != "available" ];
    do
        sleep 15
    done

    aws ec2 terminate-instances \
        --instance-ids ${BUILDER_ID} \
        --region ${REGION}
}

main() {
    set -x
    create_build_image_instance
    while [ "$(aws ec2 describe-instance-status --instance-ids ${BUILDER_ID} --include-all-instances --query='InstanceStatuses[0].InstanceState.Name' --output text --region ${REGION})" != "stopped" ];
    do
        sleep 5
    done
    create_worker_image
}

confirm "Building image $WORKER_IMAGE with properties:\n Version: ${WORKER_IMAGE_VERSION}\n Region: ${REGION}" && main
