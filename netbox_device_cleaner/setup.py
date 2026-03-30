from setuptools import setup, find_packages

setup(
    name='netbox_device_cleaner',
    version='1.0.0',
    description='Plugin NetBox — purge complète des objets liés à un équipement',
    author='Squad LAN DC',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[],
    package_data={
        'netbox_device_cleaner': [
            'templates/netbox_device_cleaner/*.html',
        ],
    },
    zip_safe=False,
)
