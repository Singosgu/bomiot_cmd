import colorama
from colorama import Fore, Style
colorama.init()


def welcome() -> None:
    """
    Welcome message
    """
    # Print welcome message with colors
    print('')
    print(f"{Fore.CYAN}  $$$$$$    $$$$$   $$$       $$$  $$   $$$$$   $$$$$$ {Style.RESET_ALL}")
    print(f"{Fore.CYAN}  $$   $$  $$   $$  $$ $     $ $$  $$  $$   $$    $$  {Style.RESET_ALL}")
    print(f"{Fore.CYAN}  $$$$$$$  $$   $$  $$  $   $  $$  $$  $$   $$    $$  {Style.RESET_ALL}")
    print(f"{Fore.CYAN}  $$   $$  $$   $$  $$   $ $   $$  $$  $$   $$    $$  {Style.RESET_ALL}")
    print(f"{Fore.CYAN}  $$$$$$    $$$$$   $$    $    $$  $$   $$$$$     $$  {Style.RESET_ALL}")
    print('')