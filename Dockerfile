FROM nvcr.io/nvidia/pytorch:23.12-py3

# Ubuntu: 20.04
# Python: 3.8
# CUDA: 12.1.0
# cuDNN: 8.9.0
# NCCL: v2.16.2-1 + FP8 Support
# PyTorch: 2.1.0a0+fe05266f

LABEL maintainer="MS-AMP"

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    autoconf \
    automake \
    bc \
    build-essential \
    curl \
    dmidecode \
    git \
    iproute2 \
    jq \
    moreutils \
    net-tools \
    openssh-client \
    openssh-server \
    sudo \
    util-linux \
    vim \
    wget \
    && \
    apt-get autoremove && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

ARG NUM_MAKE_JOBS=
ENV PATH="${PATH}" \
    LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

WORKDIR /opt/msamp

ADD third_party third_party
RUN cd third_party/msccl && \
    make -j ${NUM_MAKE_JOBS} src.build NVCC_GENCODE="\
    -gencode=arch=compute_80,code=sm_80 \
    -gencode=arch=compute_90,code=sm_90" && \
    make install
# cache TE build to save time in CI
#-gencode=arch=compute_70,code=sm_70 \
ENV MAX_JOBS=1
RUN python3 -m pip install --upgrade pip && \
    python3 -m pip install flash-attn==1.0.9 git+https://github.com/NVIDIA/TransformerEngine.git@v0.11

#ADD . .
RUN git clone https://github.com/Azure/MS-AMP.git .
ENV SETUPTOOLS_USE_DISTUTILS=local
RUN python3 -m pip install . && \
    make postinstall

ENV LD_PRELOAD="/usr/local/lib/libmsamp_dist.so:/usr/local/lib/libnccl.so:${LD_PRELOAD}"
