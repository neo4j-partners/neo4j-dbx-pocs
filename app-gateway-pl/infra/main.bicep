// ============================================================================
// Application Gateway v2 POC for Databricks Serverless → Neo4j Aura BC
//
// Deploys: VNet (2 subnets), Public IP, Application Gateway v2 (TCP + HTTP),
//          and Private Link configuration on the gateway frontend.
//
// Layer 4 TCP mode preserves TLS SNI end-to-end for Bolt traffic on 7687.
//
// Private Link validation requires an L7 httpListener on the frontend IP.
// A minimal HTTP listener on port 80 satisfies this requirement. The actual
// Bolt traffic flows through the L4 TCP listener on 7687. The experiment:
// does the PL tunnel forward all ports on the frontend IP, or only L7 ports?
// ============================================================================

@description('Azure region for all resources')
param location string = 'westus3'

@description('Prefix for resource names (combined with PL config name must be < 70 chars)')
param prefix string = 'aurabc-appgw'

@description('Neo4j Aura BC FQDN (e.g. xxxxxxxx.databases.neo4j.io)')
param auraFqdn string

@description('Neo4j Aura BC port')
param auraPort int = 7687

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------
var vnetName = '${prefix}-vnet'
var publicIpName = '${prefix}-pip'
var appGwName = '${prefix}-gw'
var plConfigName = 'pl-config'

// ---------------------------------------------------------------------------
// Public IP for Application Gateway (static, Standard SKU)
//
// Required for two reasons:
//   1. Outbound internet connectivity to reach Aura BC
//   2. Private Link configuration (not supported on private-only gateways)
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
// Virtual Network with 2 subnets
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

  // Application Gateway requires a dedicated subnet with delegation
  // (required since May 5, 2025)
  resource appGwSubnet 'subnets' = {
    name: 'appgw-subnet'
    properties: {
      addressPrefix: '10.0.1.0/24'
      delegations: [
        {
          name: 'appgw-delegation'
          properties: {
            serviceName: 'Microsoft.Network/applicationGateways'
          }
        }
      ]
    }
  }

  // Private Link subnet — network policies must be disabled
  resource plSubnet 'subnets' = {
    name: 'appgw-pl-subnet'
    properties: {
      addressPrefix: '10.0.2.0/24'
      privateLinkServiceNetworkPolicies: 'Disabled'
    }
    dependsOn: [appGwSubnet]
  }
}

// ---------------------------------------------------------------------------
// Application Gateway v2 — Layer 4 TCP mode
//
// TCP listener + TCP backend setting = TLS passthrough. The gateway treats
// the data stream as opaque bytes; the client's TLS handshake (including
// SNI) passes through untouched to Aura BC.
//
// Note: Layer 4 support is labeled "in Preview" on the components page as
// of March 2026. Verify GA status with Microsoft before production use.
// ---------------------------------------------------------------------------
resource appGw 'Microsoft.Network/applicationGateways@2025-01-01' = {
  name: appGwName
  location: location
  properties: {
    sku: {
      name: 'Standard_v2'
      tier: 'Standard_v2'
    }
    autoscaleConfiguration: {
      minCapacity: 1
      maxCapacity: 2
    }
    gatewayIPConfigurations: [
      {
        name: 'appgw-ip-config'
        properties: {
          subnet: {
            id: vnet::appGwSubnet.id
          }
        }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'appgw-frontend-ip'
        properties: {
          publicIPAddress: {
            id: publicIp.id
          }
          privateLinkConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/privateLinkConfigurations', appGwName, plConfigName)
          }
        }
      }
    ]
    frontendPorts: [
      {
        name: 'bolt-port'
        properties: {
          port: auraPort
        }
      }
      {
        name: 'http-port'
        properties: {
          port: 80
        }
      }
    ]

    // --- Layer 7 HTTP listener (required for Private Link validation) ---
    // Private Link on App Gateway only validates against httpListeners.
    // This minimal HTTP listener on port 80 satisfies that check.
    httpListeners: [
      {
        name: 'pl-http-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appgw-frontend-ip')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'http-port')
          }
          protocol: 'Http'
        }
      }
    ]

    backendHttpSettingsCollection: [
      {
        name: 'pl-http-backend-setting'
        properties: {
          port: 80
          protocol: 'Http'
          requestTimeout: 30
        }
      }
    ]

    requestRoutingRules: [
      {
        name: 'pl-http-routing-rule'
        properties: {
          priority: 200
          ruleType: 'Basic'
          httpListener: {
            id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'pl-http-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'aura-bc-backend-pool')
          }
          backendHttpSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', appGwName, 'pl-http-backend-setting')
          }
        }
      }
    ]

    // --- Layer 4 TCP listener (carries the actual Bolt traffic) ---

    listeners: [
      {
        name: 'bolt-tcp-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appgw-frontend-ip')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'bolt-port')
          }
          protocol: 'Tcp'
        }
      }
    ]

    backendAddressPools: [
      {
        name: 'aura-bc-backend-pool'
        properties: {
          backendAddresses: [
            {
              fqdn: auraFqdn
            }
          ]
        }
      }
    ]

    backendSettingsCollection: [
      {
        name: 'bolt-tcp-backend-setting'
        properties: {
          port: auraPort
          protocol: 'Tcp'
          timeout: 300
        }
      }
    ]

    routingRules: [
      {
        name: 'bolt-routing-rule'
        properties: {
          priority: 100
          ruleType: 'Basic'
          listener: {
            id: resourceId('Microsoft.Network/applicationGateways/listeners', appGwName, 'bolt-tcp-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'aura-bc-backend-pool')
          }
          backendSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendSettingsCollection', appGwName, 'bolt-tcp-backend-setting')
          }
        }
      }
    ]

    // --- Private Link configuration ---
    // Enabling Private Link on the frontend IP causes a brief traffic
    // disruption (< 1 minute). Deploy during initial setup, not on a
    // live gateway carrying production traffic.
    privateLinkConfigurations: [
      {
        name: plConfigName
        properties: {
          ipConfigurations: [
            {
              name: 'pl-ip-config'
              properties: {
                primary: true
                privateIPAllocationMethod: 'Dynamic'
                subnet: {
                  id: vnet::plSubnet.id
                }
              }
            }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output appGatewayResourceId string = appGw.id
output appGatewayPublicIp string = publicIp.properties.ipAddress
output privateLinkConfigName string = plConfigName
output appGatewayName string = appGw.name
output resourceGroupName string = resourceGroup().name
