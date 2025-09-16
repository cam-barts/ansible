# Ansible Repository Setup for Dotfiles and Host Management

This document outlines the plan to build an Ansible repository integrated with my existing [dotfiles repository](https://github.com/cam-barts/.dotfiles). The goal is to automate configuration across various hosts, each belonging to different groups with specific requirements.

## Host Groups and Configuration Targets

### 1. Raspberry Pis

-   **Architecture:** ARMv7    
-   **OS:** Hypriot, Raspbian OS
-   Special considerations:
    -   Possibly exclude certain software like Obsidian due to hardware constraints
    -   Configure with lightweight setups suitable for ARM devices

### 2. Development Machines

-   **OS:** Typically Arch Linux or Arch-based distributions
-   Focus on:
    -   Installing development tools
    -   Synchronizing dotfiles and common software

### 3. HackTheBox PwnBox

-   **OS:** Parrot (Debian-based)    
-   Used for practicing on HTB platform

### 4. Work Machines

-   **OS:** Windows
-   Consider:
    -   WSL (Windows Subsystem for Linux) configurations for Linux tools and environments
    -   Setting up Windows-specific software

## Common Software to Install & Configure

| Software                                      | Notes                        | Customization/Config Source    |
| --------------------------------------------- | ---------------------------- | ------------------------------ |
| [Obsidian](https://obsidian.md)               | Not for Raspberry Pis        | Use only on applicable hosts   |
| [Mask](https://github.com/jacobdeichert/mask) |                              | Standard installation          |
| [Starship.rs](http://starship.rs)             | Shell prompt                 | With my custom dotfiles config |
| Kitty terminal                                |                              | With my dotfiles config        |
| Neovim                                        |                              | With my dotfiles config        |
| [Stow](https://www.gnu.org/software/stow/)    | Package manager for dotfiles |                                |
| [Hosts](https://github.com/xwmx/hosts)        | Manage `/etc/hosts`          | Used mostly on the PwnBox      |
| [Atuin](https://atuin.sh/)                    | Shell history management     |                                |


## Fonts for Enhanced UX

-   [**Atkinson Hyperlegible**](https://www.brailleinstitute.org/freefont/): Used across most applications for clarity (excluding terminal)
-   [**MonaSpace**](https://github.com/githubnext/monaspace): For terminal fonts, customized through Kitty config within dotfiles repo

## PwnBox Configuration & Setup

-   Refer to [HackTheBox PwnBox guide](https://help.hackthebox.com/en/articles/7913354-mastering-pwnbox) for specifics
-   Initial setup steps:
    -   Ensure `ansible` is installed on the host machine via `user_init` script
    -   Use `ansible-pull` to fetch and run configuration playbooks
        - `ansible-pull -U https://github.com/cam-barts/ansible.git -d ~/ansible-pwnbox -i pwnbox -C main playbooks/configure_pwnbox.yml`

### TO DOs
- [ ] Install `ansible` during initial setup (`user_init`)
- [ ] Use `ansible-pull` to synchronize configurations
- [ ] Set up [AutoRecon](https://github.com/Tib3rius/AutoRecon) with markdown output into the Obsidian vault stored in `my_data` (which is synced via sshfs)

## Next Steps

-   Structure the Ansible inventory with host groups matching the above categories
-   Develop playbooks for:
    -   Base system setup
    -   Dotfiles deployment (leveraging `stow`)
    -   Software installation (e.g., Obsidian, Mask, starship, kitty, nvim)
    -   Font management
    -   Windows-specific setup and WSL configurations
-   Automate initial PwnBox environment configuration and synchronize tools like AutoRecon and Mask
