/* Forward declarations for third_party/dhrystone, force-included with -include.
 *
 * Dhrystone is K&R C: it calls its own Proc_* and Func_* routines before defining
 * them and lets C89 declare them implicitly as returning int. Declaring them here,
 * in the same K&R form and with the same int result (Boolean is int), lets the
 * build keep implicit function declarations an error, so a library call our
 * stand-in headers forgot to declare fails the build instead of guessing a type.
 */
#ifndef RV32_DHRYSTONE_PROCS_H
#define RV32_DHRYSTONE_PROCS_H
int Proc_1();
int Proc_2();
int Proc_3();
int Proc_4();
int Proc_5();
int Proc_6();
int Proc_7();
int Proc_8();
int Func_2();
int Func_3();
#endif
