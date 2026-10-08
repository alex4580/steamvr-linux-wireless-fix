/* Experimental exact-build adapter; see the repository's validation notes. */
.text
.global compact_getzipitem
.type compact_getzipitem,@function
compact_getzipitem:
    push %rbp
    mov %rsp,%rbp
    push %rbx
    push %r12
    push %r13
    sub $0x1038,%rsp
    mov %rdx,%rbx
    mov %rdi,%r12
    mov %esi,%r13d
    test %rbx,%rbx
    jz .Linvalid
    /* Initialize count-only/error-path fields to deterministic values. */
    mov %rsp,%rdi
    xor %eax,%eax
    mov $0x206,%ecx
    rep stosq
    mov %r12,%rdi
    mov %r13d,%esi
    mov %rsp,%rdx
    call original_getzipitem
    test %eax,%eax
    jnz .Ldone
    /* Reject names that cannot fit, before writing the compact destination. */
    lea 4(%rsp),%rsi
    xor %edx,%edx
.Lscan:
    cmpb $0,(%rsi,%rdx)
    je .Lcopy
    inc %edx
    cmp $260,%edx
    jb .Lscan
.Linvalid:
    mov $0x10000,%eax
    jmp .Ldone
.Lcopy:
    mov %rbx,%rdi
    mov $0x27,%ecx
    xor %eax,%eax
    rep stosq
    mov (%rsp),%eax
    mov %eax,(%rbx)
    lea 4(%rbx),%rdi
    lea 1(%rdx),%rcx
    rep movsb
    mov 0x1004(%rsp),%eax
    mov %eax,0x108(%rbx)
    mov 0x1008(%rsp),%rax
    mov %rax,0x110(%rbx)
    mov 0x1010(%rsp),%rax
    mov %rax,0x118(%rbx)
    mov 0x1018(%rsp),%rax
    mov %rax,0x120(%rbx)
    mov 0x1020(%rsp),%rax
    mov %rax,0x128(%rbx)
    mov 0x1028(%rsp),%rax
    mov %rax,0x130(%rbx)
    xor %eax,%eax
.Ldone:
    add $0x1038,%rsp
    pop %r13
    pop %r12
    pop %rbx
    pop %rbp
    ret
.size compact_getzipitem,.-compact_getzipitem
.section .note.GNU-stack,"",@progbits
