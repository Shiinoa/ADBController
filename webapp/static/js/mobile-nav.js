function setMobileSidebarExpanded(expanded) {
    document.querySelectorAll('.mobile-menu-toggle').forEach((button) => {
        button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    });
}

function openMobileSidebar() {
    document.body.classList.add('sidebar-open');
    setMobileSidebarExpanded(true);
}

function closeMobileSidebar() {
    document.body.classList.remove('sidebar-open');
    setMobileSidebarExpanded(false);
}

function toggleMobileSidebar() {
    if (document.body.classList.contains('sidebar-open')) {
        closeMobileSidebar();
    } else {
        openMobileSidebar();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.sidebar .nav-link').forEach((link) => {
        link.addEventListener('click', () => {
            if (window.innerWidth <= 1024) {
                closeMobileSidebar();
            }
        });
    });

    window.addEventListener('resize', () => {
        if (window.innerWidth > 1024) {
            closeMobileSidebar();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeMobileSidebar();
        }
    });
});
