Design a scheduler and CLI for a remote silicon bringup platform.

# Background context
The setup premise is as follows:
We will have a custom designed PCB with an FPGA and the physical silicon under test ("SoC") on that custom PCB. That PCB will have variable power control (via a RP2350), FPGA will flashed via uart_tsi, spi_tsi, FIFO, the SoC will be talked to by the FPGA, and we should be able to talk to the SoC via JTAG and UART. The working/incomplete PCB firmware repository is here: https://github.com/ucb-bar/lilikoi. The custom PCB and SoC will be connected to an Apple Mac Mini. Each Mac Mini can house up to 3 of these custom PCB + SoC bringup platforms. There will be multiple Mac Minis ("clusters") that we can schedule work to. This is all background information and not strictly necessary for this project. For this project, we will assume that a series of shell scripts or python scripts (CLI) will be provided to interact with the firmware.

For the firmware scripts, leave enough flexibility such that we can stich this together once the firmware is complete.

# Scope of this project
We will need to build the software side that will go on the Mac Mini and the scheduler that will live in the cloud/on another machine.

## Available functions
Setup -- the following definitions are mapped to each Mac Mini, identifying what is on each cluster:
{
    Boards: [
        {
            Board 1, a unique id identifying the board,
            /dev/XYZ mapping: "",
            Num vrails,
            Num Vsense, ISense,
            Clock used by the SoC: [On-Chip, FPGA, External_clock_gen]
            FPGAs_on_board: [
                xilinx model/part num: "",
                /dev/xyz mapping: ""
            ]
            SoC_on_board: [
                name_of_soc: "", // for users: this should be the same name used by the Baremetal IDE config to prevent confusion
                /dev/xyz mapping: ""
            ]
        } // boards will be connected by USB (USB-C)
        {Board 2, a unique id identifying the board} // boards will be connected by USB (USB-C)
        {Board 3, a unique id identifying the board} // boards will be connected by USB (USB-C)
        // a mac mini can have less than 3 boards connected, in which case that struct should be null.
    ],
    env: [
        Optional: Vivado: [
            vivado version: "",
            vivado path: "", 
            vivado type: [lab or full]
        ],
        Required: openocd path: "",
        Optional: OpenFPGA Loader path: "",
        Optional: RISC-V toolchain path: "",
        Optional: RISC-V GCC Version: "",
        Optional: Chipyard path: "",
        System GCC Version: "",
        System Clang Version: "",
    ],
    "OS, release, build",
    Apple device model # "A number",
    "MAC Address"
}

Job -- the following fields define a job submitted to the custom PCB:
{
    "owner": "ldap name",
    "date submitted:"unix timestamp",
    "priority": "niceness-like value",
    "resource_constraints": [
        "board": "board mapping" // this is the 1 key that will define FPGA & SoC fields below, if not specified, we try to locate a board by the following 2 constraints -- board value also determines which board's queue we submit the job to. If this constraint = all 3 boards, we pick whichever board queue is shortest
        "SoC": "",
        "FPGA": "", // this should rarely change, unless we have a fletcherlake PCB v2 with a different FPGA
    ]
    "run_timeout": "int seconds", // max 35 days = 5 weeks
    "run_collateral_ttl" "days", // how long to store the job's collateral: elf, bitstreams for. max 365 days.
    "shmoo": [
        NULL, OR:
        voltage: [
            - low
            - high
            - step
        ],
        frequency: [
            -low
            -upper
            -step
        ],
        binary: "path to elf",
        bitstream: "path to bitstream for FPGA",
    ]
}

## Interface on the Mac Mini
We will need to develop a CLI or TUI that runs on each Mac Mini that unifies the various firmware scripts we've been given.

This CLI/TUI will be responsible for receiving information from the scheduler, and mapping it down to the various firmware scripts we've been given.

This CLI/TUI will also be responsible for setting up the Mac Mini initially (including setting up envs, installing deps, one time provisioning & registration with the scheduler as an up cluster, etc).

This CLI or TUI should also have a python package binding (API) such that power users can write custom python scripts to directly interact with this "CLI/TUI" without passing flags or doing exec() in their python scripts etc.

### queues on the mac mini
* 1 job queue for each board
* a deletion queue? or cron jobs? for job collateral TTL/deletion -- this needs to presist across hard crashes of the machine (power loss, etc -- so not sure if our software should manage this or we should do this through managing system cron jobs, but either way we probably need a data structure to store the owners of the collateral that is about to get deleted, so we don't end up parsing the job json above every time in our dashboard below)

### command functionality on the mac mini ("if you sshed into the mac mini directly, youd be able to run this")
* fl represents the command to trigger the CLI living on the mac mini
**SETUP:**
fl cluster setup init:
1. ensure mac mini is networked
1. check that it is not already a cluster (does the sha of the config exist? AND does a cluster config file exist? -- if cluster config exists and no sha file exists, we are in reconfigure mode, if both dont exist, it is a new cluster)
1. user needs to pass in some data/way to join the VPC.
1. If the user does not specify a field in "Env" or "OS" or "apple device model # A number", the command should do a best attempt to fill out the fields (OTHER THAN the "Boards" field), searching typical locations for these things.  -- any user passed in field should take priority/override the automatic detection
2. then in CLI mode, look at flags for the boards defintion, in TUI mode, prompt the user to enter the boards field
3. in TUI mode, when we get to the env, os release, build, apple device model, etc fields, show the user the pre-filled/autodetected field, and let the user over ride it (ie show a grey-ish text box with the autodetected contents, if the user types over it, then its a override)
4. after the command finishes, the file is saved to a suitable location on system (globally, not in a user's folder on the system). then provide the user an absolute path to edit any fields in a text editor as needed. 
5. join the VPC, then initiates a websocket/socket io connection to the scheduler app, then transmit the setup struct.

fl cluster setup confirm:
1. acq sudo perms (via user prompt or if the user already has sudo)
2. chmod the cluster such that it requires sudo permission to edit
3. generate a hash of the cluster config, which is stored next to the file, with the same restrictive permissions
4. pixi install deps

fl cluster setup reconfigure {optional: --interactive, --freeform}:
1. acq sudo perms
2. relax permissions on the file
3. either via CLI, edit the config according to flags passed in, or if --interactive, use the tui to guide the user through editing each field, or --freeform, provide the user the absolute path to the file for them to edit in their text editor.
    3a: keep a status presistent status locally that this file is actively being edited, such that if we lose power or something in the reconfiguration stage, we know if we are in the reconfiguration stage or not -- this can simply be done by rm -rf-ing the sha when we enter reconfiguration stage (so we know we're in reconfig/editing stage if theres no sha).
4. pixi deps sync

fl custer restart:
1. acquire sudo perms
1. call any firmware scripts necessary to power down PCB, FPGA, etc 
2. issue a message to the scheduler that we're restarting (see scheduler section below)
3. restart the mac mini

fl cluster destroy:
1. acq sudo perms
2. uninstalls pixi deps
3. rm rfs the cluster definition file
4. if on our VPC, schedules a task in 30 seconds that removes the current device from our tailscale VPC
5. in that 30 second window, return information to the scheduler 
6. disconnect
7. in 30 seconds, the device should disappear from our VPC

**CLUSTER STATUS:**

fl cluster status (optional --dashboard):

htop like dashboard with tabs, displaying:
* which boards/fpgas/socs are hooked up to the mac mini

* which jobs are running, whos the owner, elapsed time
* each board's queue

* collateral deletion time/time left until deletion for all the collateral on the mac mini (along with their owners)

* mac mini status:
 - storage
 - cpu load
 - mem load


**Submitting Jobs to a board:**
fl job submit (-run-binary-only, -force-reflash-bitstream)

* submit a job to a board's queue. the queue is passed in to the command.
* notify the scheduler that the job has been scheduled.

fl job kill
* remove from the board queue if it is not actively being run
* if it is being actively ran on the board, run any firmware scripts to "kill" the job.
* report back to the scheduler.


**Actually running a job:**
* Shmoo logic should be a binary search from high to low, with voltage increasing by `step`. 
    - If at high voltage the test doesn't run, then its an immediate fail, dont even run the voltage sweep or freq sweep, report failure with code "binary failed to run"
    - Frequency uses the same logic, but low to high freq binary search, defined by `step`.
* handling bitstreams:
    - a fingerprint (sha) file should be kept of what bitstream is flashed on the FPGA associted to which (of the 3) boards. If the new bitstream has a different sha fingerprint, call the supplied firmware script to program the FPGA.
    - this figerprint script should disappear on a power loss (or restart) of the mac mini (we probably want to leverage the OS to make this happen?)
* Binary logic is:
    - call the necessary firmware script to flash the binary onto the SoC via UART TSI (these firmware script will be supplied)

## Scheduler
The scheduler will submit jobs to a Mac Mini cluster that is suitable/fitting our constraints given by the user in the scheduler.

The scheduler will have:
* firewall whitelist of IP addresses that can access the scheduler (if IP address is not in the list, it shouldnt even resolve -- port should be closed)
* a front end web app, users authenticate through oauth, their emails need to be on a whitelist. The whitelist should not be stored on the scheduler machine (is there a service for this? or just use a mongodb?)

Scheduler should be the master of a tailscale (or alternative service) VPC:
* encrypted socket io (web socket) connection to each mac mini, enabling full duplex, streaming communication for messages/status updates/TTL collateral notifications
* schedule should be able to be deployed on any server (AWS, GCP, or local on prem server) -- so will need to join VPC.

Authenticated users will be able to see a dashboard of all the machines, who is running what, queue status of each machine, TTL of collateral on each machine. -- make use of the "CLI/TUI" and its API for this, don't recompute everything.

**Scheduler notification services:**
email service (for job success, fail, hangs, downtime, collateral job deletion), via mailgun
slack + google chat integration

**Setting up a new machine:**
* a button on the web dashboard, once clicked, should give the user some fields (maybe a tailscale link??) to pass into `fl cluster setup init` such that it can be used to join the VPC.
* the client (new mac mini) will set everything up and attempt to connect to the scheduler via a new socket io connection. upon connection, the scheduler will get the setup definition struct.

**submitting a job to the scheduler:**
have a port in Chipyard where users can authenticate on whatever server they're currently on and call a command to submit a job.
* the critical thing here is on any arbitary machine, without joining any VPCs permenantly and without sudo permissions, users need to be able to authenticate via oauth and submit a job to the scheduler. 
* this method will tolerate large binaries and bitstreams, using stanford SLAC BBCP for transfering large files.
* the challenge here is without joining the user's current machine (where they are submitting from) onto any VPC permantly or in a way that requires sudo permissions, we need to a) given the resource_constraints, ask the web scheduler to give us a machine + queue to schedule to schedule to, b) make that connection to that mac mini, BBCP the files over and run the fl job submit command.
* perhaps we use a proxy of some sort? which is not hard coded into chipyard, but setup by the user when they call init (./fl-client init) on the chipyard port.

## Networking
All mac minis will need to join a tailscale VPC, no matter whether they are registered on local compute (with a static ipv4) or not.

VPC will be run by the scheduler

Vivado licenses will need to come from our BWRC compute... the scheduler will need to forward the license server into our custom VPC
