// ============================================================================
// Load Balancer + Reverse Proxy POC for Databricks Serverless → Neo4j Aura BC
//
// Deploys: VNet (3 subnets), NAT Gateway, NSG, proxy VM with HAProxy,
//          Internal Standard LB, and Private Link Service.
// ============================================================================

@description('Azure region for all resources')
param location string = 'westus3'

@description('Prefix for resource names')
param prefix string = 'aurabc-lb'

@description('Neo4j Aura BC FQDN (e.g. xxxxxxxx.databases.neo4j.io)')
param auraFqdn string

@description('Neo4j Aura BC port')
param auraPort int = 7687

@description('VM admin username')
param adminUsername string = 'azureuser'

@description('SSH public key for VM access')
@secure()
param sshPublicKey string

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------
var vnetName = '${prefix}-vnet'
var nsgName = '${prefix}-proxy-nsg'
var natGwName = '${prefix}-natgw'
var publicIpName = '${prefix}-natgw-pip'
var nicName = '${prefix}-proxy-nic'
var vmName = '${prefix}-proxy-vm'
var lbName = '${prefix}-ilb'
var plsName = '${prefix}-pls'

// ---------------------------------------------------------------------------
// Public IP for NAT Gateway (static, Standard SKU)
// ---------------------------------------------------------------------------
resource publicIp 'Microsoft.Network/publicIPAddresses@2025-01-01' = {
  name: publicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
    idleTimeoutInMinutes: 4
  }
}

// ---------------------------------------------------------------------------
// NAT Gateway
// ---------------------------------------------------------------------------
resource natGateway 'Microsoft.Network/natGateways@2025-01-01' = {
  name: natGwName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: publicIp.id
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Network Security Group — proxy subnet
// ---------------------------------------------------------------------------
resource nsg 'Microsoft.Network/networkSecurityGroups@2025-01-01' = {
  name: nsgName
  location: location
  properties: {
    securityRules: [
      {
        name: 'Allow-Bolt-Inbound-VNet'
        properties: {
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '${auraPort}'
        }
      }
      {
        name: 'Allow-Bolt-Inbound-LB'
        properties: {
          priority: 110
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '${auraPort}'
        }
      }
      {
        name: 'Allow-SSH-Inbound'
        properties: {
          priority: 120
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Virtual Network with 3 subnets
// ---------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2025-01-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
  }

  resource lbSubnet 'subnets' = {
    name: 'lb-subnet'
    properties: {
      addressPrefix: '10.0.1.0/24'
    }
  }

  resource proxySubnet 'subnets' = {
    name: 'proxy-subnet'
    properties: {
      addressPrefix: '10.0.2.0/24'
      networkSecurityGroup: {
        id: nsg.id
      }
      natGateway: {
        id: natGateway.id
      }
    }
    dependsOn: [lbSubnet]
  }

  resource plsNatSubnet 'subnets' = {
    name: 'pls-nat-subnet'
    properties: {
      addressPrefix: '10.0.3.0/24'
      privateLinkServiceNetworkPolicies: 'Disabled'
    }
    dependsOn: [proxySubnet]
  }
}

// ---------------------------------------------------------------------------
// Internal Standard Load Balancer
// ---------------------------------------------------------------------------
resource loadBalancer 'Microsoft.Network/loadBalancers@2025-01-01' = {
  name: lbName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    frontendIPConfigurations: [
      {
        name: 'lb-frontend'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: vnet::lbSubnet.id
          }
        }
      }
    ]
    backendAddressPools: [
      {
        name: 'lb-backend-pool'
      }
    ]
    probes: [
      {
        name: 'tcp-bolt-probe'
        properties: {
          protocol: 'Tcp'
          port: auraPort
          intervalInSeconds: 15
          numberOfProbes: 2
        }
      }
    ]
    loadBalancingRules: [
      {
        name: 'bolt-rule'
        properties: {
          protocol: 'Tcp'
          frontendPort: auraPort
          backendPort: auraPort
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIPConfigurations', lbName, 'lb-frontend')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', lbName, 'lb-backend-pool')
          }
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', lbName, 'tcp-bolt-probe')
          }
          enableTcpReset: true
          idleTimeoutInMinutes: 30
          loadDistribution: 'SourceIP'
          disableOutboundSnat: true
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Network Interface — proxy VM (NIC-based backend pool for PLS compatibility)
// ---------------------------------------------------------------------------
resource nic 'Microsoft.Network/networkInterfaces@2025-01-01' = {
  name: nicName
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: vnet::proxySubnet.id
          }
          privateIPAllocationMethod: 'Dynamic'
          loadBalancerBackendAddressPools: [
            {
              id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', lbName, 'lb-backend-pool')
            }
          ]
        }
      }
    ]
    enableAcceleratedNetworking: false
    enableIPForwarding: false
  }
  dependsOn: [loadBalancer]
}

// ---------------------------------------------------------------------------
// Proxy VM — Ubuntu 22.04 + HAProxy via cloud-init
// ---------------------------------------------------------------------------
resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: 'Standard_B2s'
    }
    storageProfile: {
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Standard_LRS'
        }
      }
      imageReference: {
        publisher: 'Canonical'
        offer: '0001-com-ubuntu-server-jammy'
        sku: '22_04-lts-gen2'
        version: 'latest'
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(replace(replace(cloudInitRaw, '__AURA_FQDN__', auraFqdn), '__AURA_PORT__', string(auraPort)))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
  }
}

var cloudInitRaw = '''#cloud-config
package_upgrade: true
packages:
  - haproxy

write_files:
  - path: /etc/haproxy/haproxy.cfg
    content: |
      global
          log /dev/log local0
          maxconn 2000
          daemon

      defaults
          log     global
          mode    tcp
          option  tcplog
          option  dontlognull
          timeout connect 10s
          timeout client  300s
          timeout server  300s
          retries 3

      frontend neo4j_bolt
          bind *:__AURA_PORT__
          default_backend aura_bc

      backend aura_bc
          server aura1 __AURA_FQDN__:__AURA_PORT__ check resolvers azure_dns

      resolvers azure_dns
          nameserver dns1 168.63.129.16:53
          resolve_retries 3
          timeout resolve 1s
          timeout retry 1s
          hold valid 30s

runcmd:
  - systemctl enable haproxy
  - systemctl restart haproxy
'''

// ---------------------------------------------------------------------------
// Private Link Service
// ---------------------------------------------------------------------------
resource privateLinkService 'Microsoft.Network/privateLinkServices@2025-01-01' = {
  name: plsName
  location: location
  properties: {
    loadBalancerFrontendIpConfigurations: [
      {
        id: resourceId('Microsoft.Network/loadBalancers/frontendIPConfigurations', lbName, 'lb-frontend')
      }
    ]
    ipConfigurations: [
      {
        name: 'pls-nat-ip-config'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Dynamic'
          privateIPAddressVersion: 'IPv4'
          subnet: {
            id: vnet::plsNatSubnet.id
          }
        }
      }
    ]
    visibility: {
      subscriptions: [
        '*'
      ]
    }
    autoApproval: {
      subscriptions: [
        '*'
      ]
    }
  }
  dependsOn: [loadBalancer]
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output natGatewayPublicIp string = publicIp.properties.ipAddress
output privateLinkServiceId string = privateLinkService.id
output privateLinkServiceName string = privateLinkService.name
output loadBalancerFrontendIp string = loadBalancer.properties.frontendIPConfigurations[0].properties.privateIPAddress
output vmName string = vm.name
output resourceGroupName string = resourceGroup().name
