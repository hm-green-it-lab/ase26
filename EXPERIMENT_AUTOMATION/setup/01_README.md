# Setup Automation

This directory contains scripts to automatically set up all required machines for the experiment environment.

---

## ⚙️ Prerequisites

Before running the setup:

* SSH access to all target machines is configured
* Required credentials are set in the root `.env` file
* Hosts and paths are configured in `paths.env`
* VPN connection is active (if required)

---

## 🚀 Run Setup

Execute the following commands from the project root:

### 1. System Under Test (SUT)

```bash
python setup/sut_setup.py
```

### 2. JMeter Load Generator

```bash
python setup/jmeter_setup.py
```

### 3. Local Machine

```bash
python setup/local_setup.py
```

---

## ⚠️ Notes

* Target directories will be created automatically
* Existing contents may be overwritten
* Do not modify generated directories after setup

---

## 🛠 Troubleshooting

### Missing Dependencies

If a required tool (e.g. `java`, `docker`, `wget`) is not detected:

1. Install it manually
2. Restart all terminals/IDEs
3. Run the setup again

---

## ✅ Summary

Configure `.env` and `paths.env`, then run:

```bash
python setup/sut_setup.py
python setup/jmeter_setup.py
python setup/local_setup.py
```
