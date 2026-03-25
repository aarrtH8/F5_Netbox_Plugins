from setuptools import setup, find_packages

setup(
    name='netbox_f5_bigip',
    version='3.3.1',
    description='Plugin NetBox pour F5 BIG-IP - Version LDB simplifiée',
    author='Squad LAN DC',
    packages=find_packages(),
    include_package_data=True,
    install_requires=['requests>=2.28.0'],
    package_data={
        'netbox_f5_bigip': [
            'templates/netbox_f5_bigip/*.html',
        ],
    },
    zip_safe=False,
)
